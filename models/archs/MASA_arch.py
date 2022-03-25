import os
import sys
# import re
import torch
import torch.nn as nn
from torch.nn import init
import torch.nn.functional as F
import functools
import copy
from functools import partial, reduce
import numpy as np
import itertools
import math
from collections import OrderedDict


def pixelUnshuffle(x, r=1):
    b, c, h, w = x.size()
    out_chl = c * (r ** 2)
    out_h = h // r
    out_w = w // r
    x = x.view(b, c, out_h, r, out_w, r)
    out = x.permute(0, 1, 3, 5, 2, 4).contiguous().view(b, out_chl, out_h, out_w)

    return out


def make_layer(block, n_layers):
    layers = []
    for _ in range(n_layers):
        layers.append(block())
    return nn.Sequential(*layers)


class ResidualBlock(nn.Module):
    def __init__(self, nf, kernel_size=3, stride=1, padding=1, dilation=1, act='relu'):
        super(ResidualBlock, self).__init__()

        self.conv1 = nn.Conv2d(nf, nf, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation)
        self.conv2 = nn.Conv2d(nf, nf, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation)

        if act == 'relu':
            self.act = nn.ReLU(inplace=True)
        else:
            self.act = nn.LeakyReLU(0.1, inplace=True)

    def forward(self, x):
        out = self.conv2(self.act(self.conv1(x)))

        return out + x


class SAM(nn.Module):
    def __init__(self, nf, use_residual=True, learnable=True):
        super(SAM, self).__init__()

        self.learnable = learnable
        self.norm_layer = nn.InstanceNorm2d(nf, affine=False)

        if self.learnable:
            self.conv_shared = nn.Sequential(nn.Conv2d(nf * 2, nf, 3, 1, 1, bias=True),
                                             nn.ReLU(inplace=True))
            self.conv_gamma = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
            self.conv_beta = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)

            self.use_residual = use_residual

            # initialization
            self.conv_gamma.weight.data.zero_()
            self.conv_beta.weight.data.zero_()
            self.conv_gamma.bias.data.zero_()
            self.conv_beta.bias.data.zero_()

    def forward(self, lr, ref):
        ref_normed = self.norm_layer(ref)
        if self.learnable:
            style = self.conv_shared(torch.cat([lr, ref], dim=1))
            gamma = self.conv_gamma(style)
            beta = self.conv_beta(style)

        b, c, h, w = lr.size()
        lr = lr.view(b, c, h * w)
        lr_mean = torch.mean(lr, dim=-1, keepdim=True).unsqueeze(3)
        lr_std = torch.std(lr, dim=-1, keepdim=True).unsqueeze(3)

        if self.learnable:
            if self.use_residual:
                gamma = gamma + lr_std
                beta = beta + lr_mean
            else:
                gamma = 1 + gamma
        else:
            gamma = lr_std
            beta = lr_mean

        out = ref_normed * gamma + beta

        return out


class Encoder(nn.Module):
    def __init__(self, in_chl, nf, n_blks=[1, 1, 1], act='relu'):
        super(Encoder, self).__init__()

        block = functools.partial(ResidualBlock, nf=nf)

        self.conv_L1 = nn.Conv2d(in_chl, nf, 3, 1, 1, bias=True)
        self.blk_L1 = make_layer(block, n_layers=n_blks[0])

        self.conv_L2 = nn.Conv2d(nf, nf, 3, 2, 1, bias=True)
        self.blk_L2 = make_layer(block, n_layers=n_blks[1])

        self.conv_L3 = nn.Conv2d(nf, nf, 3, 2, 1, bias=True)
        self.blk_L3 = make_layer(block, n_layers=n_blks[2])

        if act == 'relu':
            self.act = nn.ReLU(inplace=True)
        else:
            self.act = nn.LeakyReLU(0.1, inplace=True)

    def forward(self, x):
        fea_L1 = self.blk_L1(self.act(self.conv_L1(x)))
        fea_L2 = self.blk_L2(self.act(self.conv_L2(fea_L1)))
        fea_L3 = self.blk_L3(self.act(self.conv_L3(fea_L2)))

        return [fea_L1, fea_L2, fea_L3]


class DRAM(nn.Module):
    def __init__(self, nf):
        super(DRAM, self).__init__()
        self.conv_down_a = nn.Conv2d(nf, nf, 3, 2, 1, bias=True)
        self.conv_up_a = nn.ConvTranspose2d(nf, nf, 3, 2, 1, 1, bias=True)
        self.conv_down_b = nn.Conv2d(nf, nf, 3, 2, 1, bias=True)
        self.conv_up_b = nn.ConvTranspose2d(nf, nf, 3, 2, 1, 1, bias=True)
        self.conv_cat = nn.Conv2d(nf * 2, nf, 3, 1, 1, bias=True)
        self.act = nn.ReLU(inplace=True)

    def forward(self, lr, ref):
        res_a = self.act(self.conv_down_a(ref)) - lr
        out_a = self.act(self.conv_up_a(res_a)) + ref

        res_b = lr - self.act(self.conv_down_b(ref))
        out_b = self.act(self.conv_up_b(res_b + lr))

        out = self.act(self.conv_cat(torch.cat([out_a, out_b], dim=1)))

        return out


class Decoder(nn.Module):
    def __init__(self, nf, out_chl, n_blks=[1, 1, 1, 1, 1, 1]):
        super(Decoder, self).__init__()

        block = functools.partial(ResidualBlock, nf=nf)

        self.conv_L3 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.blk_L3 = make_layer(block, n_layers=n_blks[0])

        self.conv_L2 = nn.Conv2d(nf * 2, nf, 3, 1, 1, bias=True)
        self.blk_L2 = make_layer(block, n_layers=n_blks[1])

        self.conv_L1 = nn.Conv2d(nf * 2, nf, 3, 1, 1, bias=True)
        self.blk_L1 = make_layer(block, n_layers=n_blks[2])

        self.merge_warp_x1 = nn.Conv2d(nf * 2, nf, 3, 1, 1, bias=True)
        self.blk_x1 = make_layer(block, n_blks[3])

        self.dram_x2 = DRAM(nf)
        self.blk_x2 = make_layer(block, n_blks[4])

        self.dram_x4 = DRAM(nf)
        self.blk_x4 = make_layer(functools.partial(ResidualBlock, nf=64), n_blks[5])

        self.conv_out = nn.Conv2d(64, out_chl, 3, 1, 1, bias=True)

        self.act = nn.ReLU(inplace=True)

        self.pAda = SAM(nf, use_residual=True, learnable=True)

    def forward(self, lr_l, warp_ref_l):
        fea_L3 = self.act(self.conv_L3(lr_l[2]))
        fea_L3 = self.blk_L3(fea_L3)
        fea_L3_up = F.interpolate(fea_L3, scale_factor=2, mode='bilinear', align_corners=False)

        fea_L2 = self.act(self.conv_L2(torch.cat([fea_L3_up, lr_l[1]], dim=1)))
        fea_L2 = self.blk_L2(fea_L2)
        fea_L2_up = F.interpolate(fea_L2, scale_factor=2, mode='bilinear', align_corners=False)

        fea_L1 = self.act(self.conv_L1(torch.cat([fea_L2_up, lr_l[0]], dim=1)))
        fea_L1 = self.blk_L1(fea_L1)

        warp_ref_x1 = self.pAda(fea_L1, warp_ref_l[2])
        fea_x1 = self.act(self.merge_warp_x1(torch.cat([warp_ref_x1, fea_L1], dim=1)))
        fea_x1 = self.blk_x1(fea_x1)
        fea_x1_up = F.interpolate(fea_x1, scale_factor=2, mode='bilinear', align_corners=False)

        warp_ref_x2 = self.pAda(fea_x1_up, warp_ref_l[1])
        fea_x2 = self.dram_x2(fea_x1, warp_ref_x2)
        fea_x2 = self.blk_x2(fea_x2)
        fea_x2_up = F.interpolate(fea_x2, scale_factor=2, mode='bilinear', align_corners=False)

        warp_ref_x4 = self.pAda(fea_x2_up, warp_ref_l[0])
        fea_x4 = self.dram_x4(fea_x2, warp_ref_x4)
        fea_x4 = self.blk_x4(fea_x4)
        out = self.conv_out(fea_x4)

        return out


class MASA(nn.Module):
    def __init__(self, args):
        super(MASA, self).__init__()
        in_chl = args.input_nc
        nf = args.nf
        n_blks = [4, 4, 4]
        n_blks_dec = [2, 2, 2, 12, 8, 4]

        self.scale = args.sr_scale
        self.num_nbr = args.num_nbr
        self.psize = 3
        self.lr_block_size = 8
        self.ref_down_block_size = 1.5
        self.dilations = [1, 2, 3]

        self.enc = Encoder(in_chl=in_chl, nf=nf, n_blks=n_blks)
        self.decoder = Decoder(nf, in_chl, n_blks=n_blks_dec)

        self.criterion = nn.L1Loss(reduction='mean')

        self.weight_init(scale=0.1)

    def weight_init(self, scale=0.1):
        for name, m in self.named_modules():
            classname = m.__class__.__name__
            if classname == 'DCN':
                continue
            elif classname == 'Conv2d' or classname == 'ConvTranspose2d':
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, 0.5 * math.sqrt(2. / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif classname.find('BatchNorm') != -1:
                if m.weight is not None:
                    m.weight.data.fill_(1)
                    m.bias.data.zero_()
            elif classname.find('Linear') != -1:
                n = m.weight.size(1)
                m.weight.data.normal_(0, 0.01)
                m.bias.data = torch.ones(m.bias.data.size())

        for name, m in self.named_modules():
            classname = m.__class__.__name__
            if classname == 'ResidualBlock':
                m.conv1.weight.data *= scale
                m.conv2.weight.data *= scale
            if classname == 'SAM':
                # initialization
                m.conv_gamma.weight.data.zero_()
                m.conv_beta.weight.data.zero_()

    def bis(self, input, dim, index):
        # batch index select
        # input: [N, C*k*k, H*W]
        # dim: scalar > 0
        # index: [N, Hi, Wi]
        views = [input.size(0)] + [1 if i != dim else -1 for i in range(1, len(input.size()))]  # views = [N, 1, -1]
        expanse = list(input.size())
        expanse[0] = -1
        expanse[dim] = -1  # expanse = [-1, C*k*k, -1]
        index = index.clone().view(views).expand(expanse)  # [N, Hi, Wi] -> [N, 1, Hi*Wi] - > [N, C*k*k, Hi*Wi]
        return torch.gather(input, dim, index)  # [N, C*k*k, Hi*Wi]

    def search_org(self, lr, reflr, ks=3, pd=1, stride=1):
        # lr: [N, C, H, W].  [N*py*px, C, k_y+2, k_x+2].  [9*16*16, 64, 8+2, 8+2]
        # reflr: [N, C, Hr, Wr].  [N*py*px, C, diameter_y+2, diameter_x+2].  [9*16*16, 64, 13+2, 13+2]

        batch, c, H, W = lr.size()
        _, _, Hr, Wr = reflr.size()

        reflr_unfold = F.unfold(reflr, kernel_size=(ks, ks), padding=0, stride=stride)  # [N, C*k*k, Hr*Wr]. [9*16*16, 64*3*3, 13*13]
        lr_unfold = F.unfold(lr, kernel_size=(ks, ks), padding=0, stride=stride)
        lr_unfold = lr_unfold.permute(0, 2, 1)  # [N, H*W, C*k*k].  [9*16*16, 8*8, 64*3*3]

        lr_unfold = F.normalize(lr_unfold, dim=2)
        reflr_unfold = F.normalize(reflr_unfold, dim=1)

        corr = torch.bmm(lr_unfold, reflr_unfold)  # [N, H*W, Hr*Wr].  [9*16*16, 8*8, 13*13]
        corr = corr.view(batch, H-2, W-2, (Hr-2)*(Wr-2))    # [9*16*16, 8, 8, 13*13]
        sorted_corr, ind_l = torch.topk(corr, self.num_nbr, dim=-1, largest=True, sorted=True)  # [N, H, W, num_nbr]

        return sorted_corr, ind_l

    def search(self, lr, reflr, ks=3, pd=1, stride=1, dilations=[1, 2, 4]):
        # lr: [N, p*p, C, k_y, k_x].  [9, 256, 64, 10, 10].  这里为什么是10，也是为什么要replicate padding，因为，dilation=4时，lr_patches取得的pixel包括[10, 10]这个pixel
        # reflr: [N, C, Hr, Wr].    [9, 64, 128, 128]

        N, C, Hr, Wr = reflr.size()
        _, _, _, k_y, k_x = lr.size()
        x, y = k_x // 2, k_y // 2    # x = 5, y = 5  
        corr_sum = 0
        for i, dilation in enumerate(dilations):
            reflr_patches = F.unfold(reflr, kernel_size=(ks, ks), padding=dilation, stride=stride, dilation=dilation)  # [N, C*ks*ks, Hr*Wr]
            lr_patches = lr[:, :, :, y - dilation: y + dilation + 1: dilation,
                                     x - dilation: x + dilation + 1: dilation]  # [N, p*p, C, ks, ks].  [9, 256, 64, 3, 3]
            lr_patches = lr_patches.contiguous().view(N, -1, C * ks * ks)  # [N, p*p, C*ks*ks].   [9, 256, 576]

            lr_patches = F.normalize(lr_patches, dim=2)
            reflr_patches = F.normalize(reflr_patches, dim=1)
            corr = torch.bmm(lr_patches, reflr_patches)  # [N, p*p, Hr*Wr].  对于不同通道的同一patch进行卷积和，每个pixel就是两个patch的相似程度
            corr_sum = corr_sum + corr

        sorted_corr, ind_l = torch.topk(corr_sum, self.num_nbr, dim=-1, largest=True, sorted=True)  # [N, p*p, num_nbr]. [9, 256, 1]
        # sorted_corr represents highest similirity score, ind_l 代表lr的一个patch与第i个ref的patch相似最高
        return sorted_corr, ind_l

    def transfer(self, fea, index, soft_att, ks=3, pd=1, stride=1):
        # fea: [N, C, H, W]
        # index: [N, Hi, Wi]
        # soft_att: [N, 1, Hi, Wi]
        scale = stride

        fea_unfold = F.unfold(fea, kernel_size=(ks, ks), padding=0, stride=stride)  # [N, C*k*k, H*W]
        out_unfold = self.bis(fea_unfold, 2, index)  # [N, C*k*k, Hi*Wi]
        divisor = torch.ones_like(out_unfold)

        _, Hi, Wi = index.size()
        out_fold = F.fold(out_unfold, output_size=(Hi*scale, Wi*scale), kernel_size=(ks, ks), padding=pd, stride=stride)
        divisor = F.fold(divisor, output_size=(Hi*scale, Wi*scale), kernel_size=(ks, ks), padding=pd, stride=stride)
        soft_att_resize = F.interpolate(soft_att, size=(Hi*scale, Wi*scale), mode='bilinear')
        out_fold = out_fold / divisor * soft_att_resize
        # out_fold = out_fold / (ks*ks) * soft_att_resize
        return out_fold

    def make_grid(self, idx_x1, idx_y1, diameter_x, diameter_y, s):  # [N, py*px], [9, 256], 15, (1,2,4)
        idx_x1 = idx_x1 * s
        idx_y1 = idx_y1 * s
        idx_x1 = idx_x1.view(-1, 1).repeat(1, diameter_x * s)
        idx_y1 = idx_y1.view(-1, 1).repeat(1, diameter_y * s)
        idx_x1 = idx_x1 + torch.arange(0, diameter_x * s, dtype=torch.long, device=idx_x1.device).view(1, -1) # 9 * 16 * 16 = 2304
        idx_y1 = idx_y1 + torch.arange(0, diameter_y * s, dtype=torch.long, device=idx_y1.device).view(1, -1) # [2304, 15]

        ind_y_l = []
        ind_x_l = []
        for i in range(idx_x1.size(0)):     # 0 ~ 9 * 16 * 16
            grid_y, grid_x = torch.meshgrid(idx_y1[i], idx_x1[i])    # [15, 15]
            ind_y_l.append(grid_y.contiguous().view(-1))
            ind_x_l.append(grid_x.contiguous().view(-1))    # [225]
        ind_y = torch.cat(ind_y_l)   # [518400]
        ind_x = torch.cat(ind_x_l)   # 9 * 16 * 16 * 15 * 15

        return ind_y, ind_x

    def forward(self, lr, ref, ref_down, gt=None):  # lr: (128 x 128), ref: (512 x 512)
        _, _, h, w = lr.size()
        px = w // self.lr_block_size       # block_size: 8, px = 16
        py = h // self.lr_block_size
        k_x = w // px                     # k_x = 8
        k_y = h // py
        _, _, h, w = ref_down.size()      # ref_down: (128 x 128)
        diameter_x = 2 * int(w // (2 * px) * self.ref_down_block_size) + 1     # diameter: 2 * (128 // (2 * 16)) * 1.5 + 1 = 13
        diameter_y = 2 * int(h // (2 * py) * self.ref_down_block_size) + 1

        lrsr = F.interpolate(lr, scale_factor=self.scale, mode='bicubic')    # scale = 4, lrsr: (512 x 512)

        fea_lr_l = self.enc(lr)      # [fea_l1, fea_l2, fea_l3]. with size: 1x, 0.5x, 0.25x
        fea_reflr_l = self.enc(ref_down)
        fea_ref_l = self.enc(ref)

        N, C, H, W = fea_lr_l[0].size()   # 9, 64, 128, 128
        _, _, Hr, Wr = fea_reflr_l[0].size()  # _, _, 128, 128

        lr_patches = F.pad(fea_lr_l[0], pad=(1, 1, 1, 1), mode='replicate')  # lr_patches's size: (9, 64, 130, 130)
        lr_patches = F.unfold(lr_patches, kernel_size=(k_y + 2, k_x + 2), padding=(0, 0),
                              stride=(k_y, k_x))  # [N, C*(k_y+2)*(k_x+2), py*px].  [9, 6400, 256]
        lr_patches = lr_patches.view(N, C, k_y + 2, k_x + 2, py * px).permute(0, 4, 1, 2, 3)  # [N, py*px, C, k_y+2, k_x+2]

        ## find the corresponding ref patch for each lr patch
        sorted_corr, ind_l = self.search(lr_patches, fea_reflr_l[0],
                                         ks=3, pd=1, stride=1, dilations=self.dilations)   # [9, 256, 64, 10, 10] and [9, 64, 128, 128]
        
        # sorted_corr represents highest similirity score, ind_l 代表lr的一个patch与第i个ref的patch相似最高
        
        ## crop corresponding ref patches
        index = ind_l[:, :, 0]  # [N, py*px]
        idx_x = index % Wr
        idx_y = index // Wr
        idx_x1 = idx_x - diameter_x//2 - 1  
        idx_x2 = idx_x + diameter_x//2 + 1
        idx_y1 = idx_y - diameter_y//2 - 1
        idx_y2 = idx_y + diameter_y//2 + 1

        mask = (idx_x1 < 0).long()   
        idx_x1 = idx_x1 * (1 - mask)   
        idx_x2 = idx_x2 * (1 - mask) + (diameter_x + 1) * mask   # 将小于7的置为0，其他的减去7得index_x1，将index_x1置为0的位置index_x2置为14，其他的加7

        mask = (idx_x2 > Wr - 1).long()   
        idx_x2 = idx_x2 * (1 - mask) + (Wr - 1) * mask   
        idx_x1 = idx_x1 * (1 - mask) + (idx_x2 - (diameter_x + 1)) * mask  # 将大于127的index_x2置为127，将index_x2置为127的位置index_x1置为113

        mask = (idx_y1 < 0).long()
        idx_y1 = idx_y1 * (1 - mask)
        idx_y2 = idx_y2 * (1 - mask) + (diameter_y + 1) * mask

        mask = (idx_y2 > Hr - 1).long()
        idx_y2 = idx_y2 * (1 - mask) + (Hr - 1) * mask
        idx_y1 = idx_y1 * (1 - mask) + (idx_y2 - (diameter_y + 1)) * mask

        ind_y_x1, ind_x_x1 = self.make_grid(idx_x1, idx_y1, diameter_x+2, diameter_y+2, 1) # [518400], 9 * 16 * 16 * 15 * 15
        ind_y_x2, ind_x_x2 = self.make_grid(idx_x1, idx_y1, diameter_x+2, diameter_y+2, 2)
        ind_y_x4, ind_x_x4 = self.make_grid(idx_x1, idx_y1, diameter_x+2, diameter_y+2, 4)

        ind_b = torch.repeat_interleave(torch.arange(0, N, dtype=torch.long, device=idx_x1.device), py * px * (diameter_y+2) * (diameter_x+2)) # [518400]
        ind_b_x2 = torch.repeat_interleave(torch.arange(0, N, dtype=torch.long, device=idx_x1.device), py * px * ((diameter_y+2)*2) * ((diameter_x+2)*2))
        ind_b_x4 = torch.repeat_interleave(torch.arange(0, N, dtype=torch.long, device=idx_x1.device), py * px * ((diameter_y+2)*4) * ((diameter_x+2)*4))

        reflr_patches = fea_reflr_l[0][ind_b, :, ind_y_x1, ind_x_x1].view(N*py*px, diameter_y+2, diameter_x+2, C).permute(0, 3, 1, 2).contiguous()  # [N*py*px, C, (radius_y+1)*2, (radius_x+1)*2]
        ref_patches_x1 = fea_ref_l[2][ind_b, :, ind_y_x1, ind_x_x1].view(N*py*px, diameter_y+2, diameter_x+2, C).permute(0, 3, 1, 2).contiguous()
        ref_patches_x2 = fea_ref_l[1][ind_b_x2, :, ind_y_x2, ind_x_x2].view(N*py*px, (diameter_y+2)*2, (diameter_x+2)*2, C).permute(0, 3, 1, 2).contiguous()
        ref_patches_x4 = fea_ref_l[0][ind_b_x4, :, ind_y_x4, ind_x_x4].view(N*py*px, (diameter_y+2)*4, (diameter_x+2)*4, C).permute(0, 3, 1, 2).contiguous()

        ## calculate correlation between lr patches and their corresponding ref patches
        lr_patches = lr_patches.contiguous().view(N*py*px, C, k_y+2, k_x+2)
        corr_all_l, index_all_l = self.search_org(lr_patches, reflr_patches,
                                              ks=self.psize, pd=self.psize // 2, stride=1) 
        index_all = index_all_l[:, :, :, 0]  # [N*p*p, k_y, k_x]. [9*16*16, 8, 8], 论文中的index_maps
        soft_att_all = corr_all_l[:, :, :, 0:1].permute(0, 3, 1, 2)  # [N*p*p, 1, k_y, k_x]. [9*16*16, 8, 8, 1] -> [9*16*16, 1, 8, 8]

        warp_ref_patches_x1 = self.transfer(ref_patches_x1, index_all, soft_att_all,
                                            ks=self.psize, pd=self.psize // 2, stride=1)  # [N*py*px, C, k_y, k_x]
        warp_ref_patches_x2 = self.transfer(ref_patches_x2, index_all, soft_att_all,
                                            ks=self.psize * 2, pd=self.psize // 2 * 2, stride=2)  # [N*py*px, C, k_y*2, k_x*2]
        warp_ref_patches_x4 = self.transfer(ref_patches_x4, index_all, soft_att_all,
                                            ks=self.psize * 4, pd=self.psize // 2 * 4, stride=4)  # [N*py*px, C, k_y*4, k_x*4]

        warp_ref_patches_x1 = warp_ref_patches_x1.view(N, py, px, C, H//py, W//px).permute(0, 3, 1, 4, 2, 5).contiguous()  # [N, C, py, H//py, px, W//px]
        warp_ref_patches_x1 = warp_ref_patches_x1.view(N, C, H, W)
        warp_ref_patches_x2 = warp_ref_patches_x2.view(N, py, px, C, H//py*2, W//px*2).permute(0, 3, 1, 4, 2, 5).contiguous()  # [N, C, py, H//py*2, px, W//px*2]
        warp_ref_patches_x2 = warp_ref_patches_x2.view(N, C, H*2, W*2)
        warp_ref_patches_x4 = warp_ref_patches_x4.view(N, py, px, C, H//py*4, W//px*4).permute(0, 3, 1, 4, 2, 5).contiguous()  # [N, C, py, H//py*4, px, W//px*4]
        warp_ref_patches_x4 = warp_ref_patches_x4.view(N, C, H*4, W*4)

        warp_ref_l = [warp_ref_patches_x4, warp_ref_patches_x2, warp_ref_patches_x1]

        out = self.decoder(fea_lr_l, warp_ref_l)
        out = out + lrsr

        if gt is not None:
            L1_loss = self.criterion(out, gt)
            loss_dict = OrderedDict(L1=L1_loss)
            return loss_dict
        else:
            return out




if __name__ == "__main__":
    pass
