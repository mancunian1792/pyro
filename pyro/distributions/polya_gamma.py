# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import math

from pyro.distributions import constraints, Gamma


class TruncatedPolyaGamma(Binomial):
    """
    This is a PolyaGamma(0, 1) distribution truncated to have finite support in
    the interval (0, 2.5). See [1] for details. As a consequence of the truncation
    the `log_prob` method is only accurate to about six decimal places. In
    addition the provided sampler is a rough approximation that is only meant to
    be used in contexts where sample accuracy is not important (e.g. in initialization).
    Broadly, this implementation is only intended for usage in cases where good
    approximations of the `log_prob` are sufficient, as is the case e.g. in HMC.

    :param tensor prototype: A prototype tensor of arbitrary shape used to determine
        the `dtype` and `device` returned by `sample` and `log_prob`.

    References

    [1] 'Bayesian inference for logistic models using Polya-Gamma latent variables'
        Nicholas G. Polson, James G. Scott, Jesse Windle.
    """
    truncation_point = 2.5
    num_log_prob_terms = 7
    num_gamma_variates = 8
    assert num_log_prob_terms % 2 == 1

    arg_constraints = {}
    support = constraints.interval(0.0, truncation_point)
    has_rsample = False

    def __init__(self, prototype, validate_args=None):
        self.dtype = prototype.dtype
        self.device = prototype.device
        super(TruncatedPolyaGamma, self).__init__(batch_shape=(), validate_args=validate_args)

    def sample(self, sample_shape=()):
        denom = torch.arange(0.5, self.num_gamma_variates).pow(2.0)
        ones = torch.ones(self.batch_shape + sample_shape + (self.num_gamma_variates,),
                          dtype=self.dtype, device=self.device)
        x = Gamma(ones, ones).sample()
        x = (x / denom).sum(-1)
        return torch.clip(x * (0.5 / math.pi ** 2), max=self.truncation_point)

    def log_prob(self, value):
        value = value.unsqueeze(-1)
        all_indices = torch.arange(0, self.num_log_prob_terms)
        two_n_plus_one = 2.0 * all_indices + 1.0
        log_terms = two_n_plus_one.log() - 1.5 * value.log() - 0.125 * two_n_plus_one.pow(2.0) / value
        even_terms = torch.index_select(log_terms, -1, all_indices[::2])
        odd_terms = torch.index_select(log_terms, -1, all_indices[1::2])
        sum_even = torch.logsumexp(even_terms, dim=-1).exp()
        sum_odd = torch.logsumexp(odd_terms, dim=-1).exp()
        return (sum_even - sum_odd).log() - 0.5 * math.log(2.0 * math.pi)