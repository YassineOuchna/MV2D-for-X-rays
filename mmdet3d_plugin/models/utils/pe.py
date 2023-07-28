# ------------------------------------------------------------------------
# Modified from PETR (https://github.com/megvii-research/PETR)
# Copyright (c) 2022 megvii-model. All Rights Reserved.
# ------------------------------------------------------------------------
# Modified from DETR3D (https://github.com/WangYueFt/detr3d)
# Copyright (c) 2021 Wang, Yue
# ------------------------------------------------------------------------
# Modified from mmdetection3d (https://github.com/open-mmlab/mmdetection3d)
# Copyright (c) OpenMMLab. All rights reserved.
# ------------------------------------------------------------------------
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from mmcv.cnn.bricks.transformer import FFN, build_positional_encoding
from mmdet.models.utils.transformer import inverse_sigmoid


def pos2posemb3d(pos, num_pos_feats=128, temperature=10000):
    scale = 2 * math.pi
    pos = pos * scale
    dim_t = torch.arange(num_pos_feats, dtype=torch.float32, device=pos.device)
    dim_t = temperature ** (2 * (dim_t // 2) / num_pos_feats)
    pos_x = pos[..., 0, None] / dim_t
    pos_y = pos[..., 1, None] / dim_t
    pos_z = pos[..., 2, None] / dim_t
    pos_x = torch.stack((pos_x[..., 0::2].sin(), pos_x[..., 1::2].cos()), dim=-1).flatten(-2)
    pos_y = torch.stack((pos_y[..., 0::2].sin(), pos_y[..., 1::2].cos()), dim=-1).flatten(-2)
    pos_z = torch.stack((pos_z[..., 0::2].sin(), pos_z[..., 1::2].cos()), dim=-1).flatten(-2)
    posemb = torch.cat((pos_y, pos_x, pos_z), dim=-1)
    return posemb


class SELayer(nn.Module):
    def __init__(self, channels, act_layer=nn.ReLU, gate_layer=nn.Sigmoid):
        super().__init__()
        self.conv_reduce = nn.Conv2d(channels, channels, 1, bias=True)
        self.act1 = act_layer()
        self.conv_expand = nn.Conv2d(channels, channels, 1, bias=True)
        self.gate = gate_layer()

    def forward(self, x, x_se):
        x_se = self.conv_reduce(x_se)
        x_se = self.act1(x_se)
        x_se = self.conv_expand(x_se)
        return x * self.gate(x_se)


class PE(nn.Module):
    def __init__(self, positional_encoding, strides, position_range, depth_num, depth_start=1, LID=True, embed_dims=256,
                 with_fpe=False, adapt_pos3d=True, no_sin_enc=False):
        super(PE, self).__init__()
        self.strides = strides
        self.position_range = position_range
        self.depth_num = depth_num
        self.depth_start = depth_start
        self.LID = LID
        self.embed_dims = embed_dims
        self.with_fpe = with_fpe

        self.position_dim = 3 * self.depth_num
        self.position_encoder = nn.Sequential(
            nn.Conv2d(self.position_dim, self.embed_dims * 4, kernel_size=1, stride=1, padding=0),
            nn.ReLU(),
            nn.Conv2d(self.embed_dims * 4, self.embed_dims, kernel_size=1, stride=1, padding=0),
        )

        self.no_sin_enc = no_sin_enc
        if not no_sin_enc:
            if adapt_pos3d:
                self.adapt_pos3d = nn.Sequential(
                    nn.Conv2d(self.embed_dims * 3 // 2, self.embed_dims * 4, kernel_size=1, stride=1, padding=0),
                    nn.ReLU(),
                    nn.Conv2d(self.embed_dims * 4, self.embed_dims, kernel_size=1, stride=1, padding=0),
                )

            self.positional_encoding = build_positional_encoding(positional_encoding)

        if self.with_fpe:
            self.fpe = SELayer(self.embed_dims)

    def position_encoding(self, img_feats, img_metas, masks=None):
        eps = 1e-3
        pad_h, pad_w, _ = img_metas[0]['pad_shape']

        t, C, H, W = img_feats.shape
        N = img_metas[0]['num_views']
        B = t // N
        # coords_h = torch.arange(H, device=img_feats.device).float() * pad_h / H
        # coords_w = torch.arange(W, device=img_feats.device).float() * pad_w / W
        coords_h = (torch.arange(H, device=img_feats.device).double() + 0.5) * pad_h / H - 0.5
        coords_w = (torch.arange(W, device=img_feats.device).double() + 0.5) * pad_w / W - 0.5

        if self.LID:
            index = torch.arange(start=0, end=self.depth_num, step=1, device=img_feats.device).double()
            index_1 = index + 1
            bin_size = (self.position_range[3] - self.depth_start) / (self.depth_num * (1 + self.depth_num))
            coords_d = self.depth_start + bin_size * index * index_1
        else:
            index = torch.arange(start=0, end=self.depth_num, step=1, device=img_feats.device).double()
            bin_size = (self.position_range[3] - self.depth_start) / self.depth_num
            coords_d = self.depth_start + bin_size * index

        D = coords_d.shape[0]
        coords = torch.stack(torch.meshgrid([coords_w, coords_h, coords_d])).permute(1, 2, 3, 0)  # W, H, D, 3
        coords = torch.cat((coords, torch.ones_like(coords[..., :1])), -1)
        coords[..., :2] = coords[..., :2] * torch.maximum(coords[..., 2:3], torch.ones_like(coords[..., 2:3]) * eps)

        img2lidars = [np.linalg.inv(img_meta['lidar2img']) for img_meta in img_metas]
        img2lidars = np.asarray(img2lidars)

        img2lidars = coords.new_tensor(img2lidars)  # (B * N, 4, 4)
        # import ipdb; ipdb.set_trace()
        coords = coords.view(1, 1, W, H, D, 4, 1).repeat(B, N, 1, 1, 1, 1, 1)
        img2lidars = img2lidars.view(B, N, 1, 1, 1, 4, 4).repeat(1, 1, W, H, D, 1, 1)
        coords3d = torch.matmul(img2lidars, coords).squeeze(-1)[..., :3]    # [B, N, W, H, D, 3]
        coords3d[..., 0:1] = (coords3d[..., 0:1] - self.position_range[0]) / (
                    self.position_range[3] - self.position_range[0])
        coords3d[..., 1:2] = (coords3d[..., 1:2] - self.position_range[1]) / (
                    self.position_range[4] - self.position_range[1])
        coords3d[..., 2:3] = (coords3d[..., 2:3] - self.position_range[2]) / (
                    self.position_range[5] - self.position_range[2])

        coords_mask = (coords3d > 1.0) | (coords3d < 0.0)
        coords_mask = coords_mask.flatten(-2).sum(-1) > (D * 0.5)    # [B, N, W, H]
        coords_mask = masks | coords_mask.permute(0, 1, 3, 2)       # [B, N, H, w]
        coords3d = coords3d.permute(0, 1, 4, 5, 3, 2).contiguous().view(B * N, -1, H, W)
        coords3d = inverse_sigmoid(coords3d, ).float()
        # print('coords3d:', coords3d.unique())
        coords_position_embeding = self.position_encoder(coords3d)
        # print('coords_position_embeding:', coords_position_embeding.unique())

        return coords_position_embeding.view(B * N, self.embed_dims, H, W), coords_mask

    def forward(self, mlvl_feats, img_metas):
        assert len(mlvl_feats) == len(self.strides)
        num_views = img_metas[0]['num_views']
        batch_size = len(img_metas) // num_views
        input_img_h, input_img_w, _ = img_metas[0]['pad_shape']

        out_feats = []

        for lvl, x in enumerate(mlvl_feats):
            masks = x.new_ones(
                (batch_size, num_views, input_img_h, input_img_w))
            for img_id in range(batch_size):
                for view_id in range(num_views):
                    img_h, img_w, _ = img_metas[img_id * num_views + view_id]['img_shape']
                    masks[img_id, view_id, :img_h, :img_w] = 0

            # interpolate masks to have the same spatial shape with x
            masks = F.interpolate(
                masks, size=x.shape[-2:]).to(torch.bool)

            coords_position_embeding, _ = self.position_encoding(x, img_metas, masks)
            if self.with_fpe:
                coords_position_embeding = self.fpe(coords_position_embeding, x)

            pos_embed = coords_position_embeding

            if not self.no_sin_enc:
                sin_embed = self.positional_encoding(masks, stride=self.strides[lvl])
                sin_embed = self.adapt_pos3d(sin_embed.flatten(0, 1))
                pos_embed = pos_embed + sin_embed

            out_feats.append(pos_embed)
        return out_feats
