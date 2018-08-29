import torch
from torch import nn
import math

import pyro
import pyro.distributions as dist
from pyro import poutine

from pyro.contrib.oed.util import (
    get_indices, tensor_to_dict, rmv, rvv, rinverse, rdiag, rtril
)


class LinearModelGuide(nn.Module):

    def __init__(self, d, w_sizes):
        """
        Guide for linear models. No amortisation happens over designs.
        Amortisation over data is taken care of by analytic formulae for
        linear models (heavy use of truth).

        :param int d: the number of designs 
        :param dict w_sizes: map from variable string names to int.
        """
        super(LinearModelGuide, self).__init__()
        # Represent each parameter group as independent Gaussian
        # Making a weak mean-field assumption
        # To avoid this- combine labels
        self.tikhonov_diag = nn.Parameter(-2.*torch.ones(sum(w_sizes.values())))
        self.scale_tril = {l: nn.Parameter(3.*torch.ones(d, p, p)) for l, p in w_sizes.items()}
        self.w_sizes = w_sizes
        self.softplus = nn.Softplus()

    def forward(self, y_dict, design, target_labels):

        y = torch.cat(list(y_dict.values()), dim=-1)
        return self.linear_model_formula(y, design, target_labels)

    def linear_model_formula(self, y, design, target_labels):

        tikhonov_diag = torch.diag(self.softplus(self.tikhonov_diag))
        xtx = torch.matmul(design.transpose(-1, -2), design) + tikhonov_diag
        xtxi = rinverse(xtx)
        mu = rmv(xtxi, rmv(design.transpose(-1, -2), y))

        # Extract sub-indices
        mu = tensor_to_dict(self.w_sizes, mu, subset=target_labels)
        scale_tril = {l: rtril(self.scale_tril[l]) if l in target_labels}

        return mu, scale_tril

    def guide(self, y_dict, design, observation_labels, target_labels):

        pyro.module("ba_guide", self)

        # Returns two dicts from labels -> tensors
        mu, scale_tril = self.forward(y_dict, design, target_labels)

        for l in target_labels:
            w_dist = dist.MultivariateNormal(mu[l], scale_tril=scale_tril[l])
            pyro.sample(l, w_dist)


class SigmoidGuide(LinearModelGuide):

    def __init__(self, d, n, w_sizes):
        super(SigmoidGuide, self).__init__(d, w_sizes)
        self.inverse_sigmoid_scale = nn.Parameter(torch.ones(n))
        self.h1_weight = nn.Parameter(torch.ones(n))
        self.h1_bias = nn.Parameter(torch.zeros(n))

    def forward(self, y_dict, design, target_labels):

        y = torch.cat(list(y_dict.values()), dim=-1)

        # Approx invert transformation on y in expectation
        y, y1m = y.clamp(1e-35, 1), (1.-y).clamp(1e-35, 1)
        logited = y.log() - y1m.log()
        y_trans = logited/.1
        y_trans = y_trans * self.inverse_sigmoid_scale
        hidden = self.softplus(y_trans)
        y_trans = y_trans + hidden * self.h1_weight + self.h1_bias

        return self.linear_model_formula(y_trans, design, target_labels)


class NormalInverseGammaGuide(LinearModelGuide):

    def __init__(self, d, w_sizes, mf=False, tau_label="tau"):
        super(NormalInverseGammaGuide, self).__init__(d, w_sizes)
        self.alpha = nn.Parameter(100.*torch.ones(d))
        self.b0 = nn.Parameter(100.*torch.ones(d))
        self.mf = mf
        self.tau_label = tau_label

    def forward(self, y_dict, design, target_labels):

        y = torch.cat(list(y_dict.values()), dim=-1)

        mu, scale_tril = self.linear_model_formula(y, design, target_labels)

        yty = rvv(y, y)
        ytxmu = rvv(y, rmv(design, mu))
        beta = self.b0 + .5*(yty - ytxmu)

        return mu, scale_tril, self.alpha, beta

    def guide(self, y_dict, design, observation_labels, target_labels):

        pyro.module("ba_guide", self)

        mu, scale_tril, alpha, beta = self.forward(y_dict, design, target_labels)

        tau_dist = dist.Gamma(alpha, beta)
        tau = pyro.sample(self.tau_label, tau_dist)
        obs_sd = 1./tau.sqrt().unsqueeze(-1).unsqueeze(-1)

        for l in target_labels:
            if self.mf:
                w_dist = dist.MultivariateNormal(mu[l], scale_tril=scale_tril[l])
            else:
                w_dist = dist.MultivariateNormal(mu[l], scale_tril=scale_tril[l]*obs_sd)
            pyro.sample(l, w_dist)


class GuideDV(nn.Module):
    def __init__(self, guide):
        super(GuideDV, self).__init__()
        self.guide = guide

    def forward(self, design, trace, observation_labels, target_labels):

        trace.compute_log_prob()
        prior_lp = sum(trace.nodes[l]["log_prob"] for l in target_labels)
        y_dict = {l: trace.nodes[l]["value"] for l in observation_labels}
        theta_dict = {l: trace.nodes[l]["value"] for l in target_labels}

        conditional_guide = pyro.condition(self.guide, data=theta_dict)
        cond_trace = poutine.trace(conditional_guide).get_trace(
                y_dict, design, observation_labels, target_labels)
        cond_trace.compute_log_prob()

        posterior_lp = sum(cond_trace.nodes[l]["log_prob"] for l in target_labels)

        return posterior_lp - prior_lp
