import torch
import torch.nn as nn
import torch.nn.functional as F
from hem.models import get_model
from hem.models.traj_embed import _NonLocalLayer
from hem.models.basic_embedding import _BottleneckConv
from torch.distributions import Normal, MultivariateNormal
import torchvision
import numpy as np


class CondVAE(nn.Module):
    def __init__(self, inflate, latent_dim, n_non_loc=2, nloc_in=1024, drop_dim=3, dropout=0.1, temp=None):
        super().__init__()
        self._l_dim = latent_dim
        self._embed = get_model('resnet')(output_raw=True, drop_dim=drop_dim)
        self._non_locs = nn.Sequential(*[_NonLocalLayer(nloc_in, nloc_in, dropout=dropout, temperature=temp) for _ in range(n_non_loc)])
        self._temp_pool = nn.Sequential(nn.Conv3d(nloc_in, latent_dim * 2, 3, stride=2), nn.BatchNorm3d(latent_dim*2), nn.ReLU(inplace=True))
        self._spatial_pool = nn.AdaptiveAvgPool2d(latent_dim * 2)
        self._enc_mean, self._enc_ln_var = nn.Linear(latent_dim * 2, latent_dim), nn.Linear(latent_dim * 2, latent_dim)

        self._concat_conv = nn.Sequential(nn.Conv2d(1024 + latent_dim, 1024 + latent_dim, 5, padding=2), 
                                nn.BatchNorm2d(1024 + latent_dim), nn.ReLU(inplace=True))
        self._inflate_layers = []
        last = 1024 + latent_dim
        for d in inflate:
            c_up = nn.ConvTranspose2d(last, d, 2, stride=2)
            c_up_ac = nn.ReLU(inplace=True)
            bottle = _BottleneckConv(d, d, 256)
            self._inflate_layers.extend([c_up, c_up_ac, bottle])
            last = d
        self._inflate_layers = nn.Sequential(*self._inflate_layers)
        self._final = nn.Conv2d(d, 3, 3, padding=1)

    def forward(self, context, use_prior=False):
        B = context.shape[0]
        prior = MultivariateNormal(torch.zeros((B, self._l_dim)).to(context.device), torch.diag_embed(torch.ones((B, self._l_dim)).to(context.device)))
        enc_embed = self._embed(context)
        dec_embed = enc_embed[:,0]

        if not use_prior:
            enc_attn = self._non_locs(enc_embed.transpose(1, 2))
            latent_embed = self._spatial_pool(self._temp_pool(enc_attn)[:,:,0])[:,:,0,0]
            mean, var = self._enc_mean(latent_embed), torch.diag_embed(torch.exp(self._enc_ln_var(latent_embed)))
            posterior = MultivariateNormal(mean, var)
            latent_samp = posterior.rsample()
            kl = torch.distributions.kl_divergence(posterior, prior)
        else:
            latent_samp, kl = prior.rsample(), 0

        latent_samp = latent_samp.unsqueeze(-1).unsqueeze(-1)
        dec_in = torch.cat((dec_embed, latent_samp.repeat((1, 1, dec_embed.shape[2], dec_embed.shape[3]))), 1)
        dec_out = self._final(self._inflate_layers(self._concat_conv(dec_in)))

        return dec_out, kl


class _VisDecoder(nn.Module):
    def __init__(self, dec_in, dec_filters=[256, 256, 128], dc_kernel_sizes=[2,5,2], c_kernel_size=3):
        super().__init__()
        pad_size = int(c_kernel_size // 2)
        f1, f2, f3 = dec_filters
        k1, k2, k3 = dc_kernel_sizes
        
        def _build_deconv(last, f, n, dc_k):
            convs = []
            for _ in range(n):
                c = nn.Conv2d(last, f, c_kernel_size, padding=pad_size)
                n, a = nn.InstanceNorm2d(f), nn.ReLU(inplace=True)
                last = f
                convs.extend([c, n, a])
            dc = nn.ConvTranspose2d(f, f, dc_k, stride=dc_k)
            n, a = nn.InstanceNorm2d(f), nn.ReLU(inplace=True)
            convs.extend([dc, n, a])
            return convs

        self._pool1 = nn.AdaptiveAvgPool2d((3, 4))
        self._dec1 = nn.Sequential(*_build_deconv(1024 + dec_in, f1, 4, k1))
        self._dec2 = nn.Sequential(*_build_deconv(f1, f2, 3, k2))
        self._dec3 = nn.Sequential(*_build_deconv(f2, f3, 2, k3))
        self._mean = nn.Conv2d(f3, 3, 3, padding=1)

    def forward(self, start_img_latent, latents):
        has_t = len(latents.shape) == 3
        pooled_img_latent = self._pool1(start_img_latent)[:,None].repeat((1, latents.shape[1], 1, 1, 1))
        in_latent = latents.unsqueeze(-1).unsqueeze(-1).repeat((1, 1, 1, 3, 4))
        x = torch.cat((pooled_img_latent, in_latent), 2)
        x = x.view([latents.shape[0] * latents.shape[1]] + list(x.shape[2:])) if has_t else x

        x = self._dec3(self._dec2(self._dec1(x)))
        mu = self._mean(x)        
        mu = mu.view([latents.shape[0], latents.shape[1]] + list(mu.shape[1:])) if has_t else mu
        return mu


class RSSM(nn.Module):
    def __init__(self, latent_dim, vis_dim=1024, state_dim=0, action_dim=8, ff_dim=256, min_std=0.01, inflate_k=[2,5,2], inflate_d=[128, 128, 64], attn_temp=None):
        super().__init__()
        
        # encoder attributes
        self._vis_enc = get_model('resnet')(output_raw=True, drop_dim=3)
        self._append_state = state_dim > 0
        self._obs_encode = nn.Linear(vis_dim + state_dim, ff_dim) if vis_dim + state_dim != ff_dim else None
        init_hidden = torch.nn.Parameter(torch.randn((1, 1, ff_dim)) * np.sqrt(2 / ff_dim), requires_grad=True)
        self.register_parameter('_init_hidden', init_hidden)
        self._attn_module = nn.Sequential(nn.Linear(ff_dim, ff_dim), nn.ReLU(inplace=True), nn.Linear(ff_dim, vis_dim))
        self._attn_temp = attn_temp if attn_temp is not None else np.sqrt(vis_dim)

        # state inference models
        self._min_std = min_std
        self._gru_in = nn.Sequential(nn.Linear(latent_dim + action_dim, ff_dim), nn.ReLU(inplace=True))
        self._gru = nn.GRU(ff_dim, ff_dim)
        self._prior_layers = nn.Sequential(nn.Linear(ff_dim, ff_dim), nn.ReLU(inplace=True))
        self._prior_mean, self._prior_lnstd = nn.Linear(ff_dim, latent_dim), nn.Linear(ff_dim, latent_dim)
        self._post_layers = nn.Sequential(nn.Linear(2 * ff_dim, ff_dim), nn.ReLU(inplace=True))
        self._post_mean, self._post_lnstd = nn.Linear(ff_dim, latent_dim), nn.Linear(ff_dim, latent_dim)

        # s_0 latent layers
        self._img_layers = nn.ReLU()
        self._img_s0_mean = nn.Sequential(nn.Conv2d(vis_dim, vis_dim, 1))
        self._img_s0_std = nn.Sequential(nn.Conv2d(vis_dim, vis_dim, 1))

        # decoder layers
        self._state_dec = nn.Sequential(nn.Linear(latent_dim, latent_dim), nn.ReLU(inplace=True), nn.Linear(latent_dim, state_dim)) if state_dim else None
        self._img_dec = _VisDecoder(latent_dim, inflate_d, inflate_k)
    
    def encode(self, images, states):
        img_feat = self._vis_enc(images)
        obs_encode = torch.mean(img_feat, (-1, -2))
        obs_encode = torch.cat((obs_encode, states), -1) if self._append_state else obs_encode
        obs_encode = self._obs_encode(obs_encode) if self._obs_encode is not None else obs_encode
        return obs_encode, img_feat

    def decode(self, start_img_latent, latent_state):
        recon = {}
        if self._state_dec is not None:
            recon['states'] = self._state_dec(latent_state)
        recon['images'] = self._img_dec(start_img_latent, latent_state)
        return recon

    def infer_states(self, images, state_obs, actions):
        rnn_hidden = self._init_hidden.repeat((1, images.shape[0], 1))
        obs_enc, img_feat = self.encode(images, state_obs)
        img_dist_proc = self._img_layers(img_feat[:,0])
        img_dist = Normal(self._img_s0_mean(img_dist_proc), F.softplus(self._img_s0_std(img_dist_proc)) + self._min_std)
        kl_img = torch.sum(torch.distributions.kl_divergence(img_dist, Normal(0, 1)), (-1, -2, -3))
        
        post_s0 = self._posterior(torch.zeros_like(obs_enc[:,0]), obs_enc[:,0])
        states, kl = [post_s0.rsample()], [kl_img, torch.sum(torch.distributions.kl_divergence(post_s0, Normal(0, 1)), -1)]
        self._gru.flatten_parameters()

        for t_a in range(actions.shape[1]):
            prior, rnn_belief, rnn_hidden = self._transition_prior(states[-1], rnn_hidden, actions[:,t_a])
            if t_a + 1 < images.shape[1]:
                posterior = self._posterior(rnn_belief, obs_enc[:,t_a+1])
                states.append(posterior.rsample())
                kl.append(torch.sum(torch.distributions.kl_divergence(posterior, prior), -1))
            else:
                states.append(prior.rsample())
        
        states = torch.cat([s[:,None] for s in states], 1)
        kl = torch.cat([k[:,None] for k in kl], 1) if kl else kl
        return img_dist.rsample(), states, kl

    def forward(self, images, states, actions, ret_recon=False):
        start_img_latent, latent_states, kl = self.infer_states(images, states, actions)
        recon = self.decode(start_img_latent, latent_states) if ret_recon else None
        
        if ret_recon:
            return recon, kl
        return latent_states

    def _posterior(self, rnn_belief, obs_enc):
        post_in = torch.cat((rnn_belief, obs_enc), 1)
        post_mid = self._post_layers(post_in)
        mean, std = self._post_mean(post_mid), F.softplus(self._post_lnstd(post_mid)) + self._min_std
        posterior = Normal(mean, std)
        return posterior

    def _transition_prior(self, prev_state, rnn_hidden, action):
        gru_in = self._gru_in(torch.cat((prev_state, action), 1))
        rnn_belief, hidden = self._gru(gru_in[None], rnn_hidden)
        prior_mid = self._prior_layers(rnn_belief[0])
        mean, std = self._prior_mean(prior_mid), F.softplus(self._prior_lnstd(prior_mid)) + self._min_std
        prior = Normal(mean, std)
        return prior, rnn_belief[0], hidden
