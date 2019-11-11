from collections import OrderedDict

from torch.distributions import biject_to, constraints
from torch.nn import Parameter

import pyro
import pyro.distributions as dist
from pyro.distributions.util import eye_like
from pyro.nn.module import PyroModule, PyroParam, PyroSample


def _get_independent_support(dist_instance):
    if isinstance(dist_instance, dist.Independent):
        return _get_independent_support(dist_instance.base_dist)
    else:
        return dist_instance.support


class Parameterized(PyroModule):
    """
    A wrapper of :class:`torch.nn.Module` whose parameters can be set
    constraints, set priors.

    Under the hood, we move parameters to a buffer store and create "root"
    parameters which are used to generate that parameter's value. For example,
    if we set a contraint to a parameter, an "unconstrained" parameter will be
    created, and the constrained value will be transformed from that
    "unconstrained" parameter.

    By default, when we set a prior to a parameter, an auto Delta guide will be
    created. We can use the method :meth:`autoguide` to setup other auto guides.
    To fix a parameter to a specific value, it is enough to turn off its "root"
    parameters' ``requires_grad`` flags.

    Example::

        >>> class Linear(Parameterized):
        ...     def __init__(self, a, b):
        ...         super(Linear, self).__init__()
        ...         self.a = Parameter(a)
        ...         self.b = Parameter(b)
        ...
        ...     def forward(self, x):
        ...         return self.a * x + self.b
        ...
        >>> linear = Linear(torch.tensor(1.), torch.tensor(0.))
        >>> linear.set_constraint("a", constraints.positive)
        >>> linear.set_prior("b", dist.Normal(0, 1))
        >>> linear.autoguide("b", dist.Normal)
        >>> assert "a_unconstrained" in dict(linear.named_parameters())
        >>> assert "b_loc" in dict(linear.named_parameters())
        >>> assert "b_scale_unconstrained" in dict(linear.named_parameters())
        >>> assert "a" in dict(linear.named_buffers())
        >>> assert "b" in dict(linear.named_buffers())
        >>> assert "b_scale" in dict(linear.named_buffers())

    Note that by default, data of a parameter is a float :class:`torch.Tensor`
    (unless we use :func:`torch.set_default_tensor_type` to change default
    tensor type). To cast these parameters to a correct data type or GPU device,
    we can call methods such as :meth:`~torch.nn.Module.double` or
    :meth:`~torch.nn.Module.cuda`. See :class:`torch.nn.Module` for more
    information.
    """
    def __init__(self):
        super(Parameterized, self).__init__()
        self._priors = OrderedDict()
        self._guides = OrderedDict()
        self._mode = None

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if isinstance(value, PyroSample):
            self._priors[name] = value.prior
            self.autoguide(name, dist.Delta)

    def autoguide(self, name, dist_constructor):
        """
        Sets an autoguide for an existing parameter with name ``name`` (mimic
        the behavior of module :mod:`pyro.infer.autoguide`).

        .. note:: `dist_constructor` should be one of
            :class:`~pyro.distributions.Delta`,
            :class:`~pyro.distributions.Normal`, and
            :class:`~pyro.distributions.MultivariateNormal`. More distribution
            constructor will be supported in the future if needed.

        :param str name: Name of the parameter.
        :param dist_constructor: A
            :class:`~pyro.distributions.distribution.Distribution` constructor.
        """
        if name not in self._priors:
            raise ValueError("There is no prior for parameter: {}".format(name))

        if dist_constructor not in [dist.Delta, dist.Normal, dist.MultivariateNormal]:
            raise NotImplementedError("Unsupported distribution type: {}"
                                      .format(dist_constructor))

        # delete old guide
        if name in self._guides:
            dist_args = self._guides[name][1]
            for arg in dist_args:
                delattr(self, "{}_{}".format(name, arg))

        p = getattr(self, name)
        if dist_constructor is dist.Delta:
            support = _get_independent_support(self._priors[name])
            if support is constraints.real:
                p_map = Parameter(p.detach())
            else:
                p_map = PyroParam(p.detach(), support)
            setattr(self, "{}_map".format(name), p_map)
            dist_args = ("map",)
        elif dist_constructor is dist.Normal:
            loc = Parameter(biject_to(self._priors[name].support).inv(p).detach())
            scale = PyroParam(loc.new_ones(loc.shape), constraints.positive)
            setattr(self, "{}_loc".format(name), loc)
            setattr(self, "{}_scale".format(name), scale)
            dist_args = ("loc", "scale")
        elif dist_constructor is dist.MultivariateNormal:
            loc = Parameter(biject_to(self._priors[name].support).inv(p).detach())
            identity = eye_like(loc, loc.size(-1))
            scale_tril = PyroParam(identity.repeat(loc.shape[:-1] + (1, 1)),
                                   constraints.lower_cholesky)
            setattr(self, "{}_loc".format(name), loc)
            setattr(self, "{}_scale_tril".format(name), scale_tril)
            dist_args = ("loc", "scale_tril")
        else:
            raise NotImplementedError

        self._guides[name] = (dist_constructor, dist_args)

    def set_mode(self, mode):
        """
        Sets ``mode`` of this object to be able to use its parameters in
        stochastic functions. If ``mode="model"``, a parameter will get its
        value from its prior. If ``mode="guide"``, the value will be drawn from
        its guide.

        .. note:: This method automatically sets ``mode`` for submodules which
            belong to :class:`Parameterized` class.

        :param str mode: Either "model" or "guide".
        """
        for module in self.modules():
            if isinstance(module, Parameterized):
                module.mode = mode

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, mode):
        self._mode = mode

    def _pyro_sample(self, name, fn):
        cache = self.__dict__['_pyro_cache']
        value = cache.get(name)
        if value is None:
            if self.mode == "guide":
                fn = self._get_guide(name.split(".")[-1], name)
            value = pyro.sample(name, fn)
            cache.set(name, value)
        return value

    def _get_guide(self, name, full_name):
        dist_constructor, dist_args = self._guides[name]

        if dist_constructor is dist.Delta:
            p_map = getattr(self, "{}_map".format(name))
            return dist.Delta(p_map, event_dim=p_map.dim())

        # create guide
        dist_args = {arg: getattr(self, "{}_{}".format(name, arg)) for arg in dist_args}
        guide = dist_constructor(**dist_args)

        # no need to do transforms when support is real (for mean field ELBO)
        if _get_independent_support(self._priors[name]) is constraints.real:
            return guide.to_event()

        # otherwise, we do inference in unconstrained space and transform the value
        # back to original space
        # TODO: move this logic to infer.autoguide or somewhere else
        unconstrained_value = pyro.sample("{}_latent".format(full_name), guide.to_event(),
                                          infer={"is_auxiliary": True})
        transform = biject_to(self._priors[name].support)
        value = transform(unconstrained_value)
        log_density = transform.inv.log_abs_det_jacobian(value, unconstrained_value)
        return dist.Delta(value, log_density.sum(), event_dim=value.dim())
