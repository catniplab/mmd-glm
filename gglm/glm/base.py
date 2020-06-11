from functools import partial
import pickle

import numpy as np

from ..optimization import NewtonMethod
from ..utils import get_dt, shift_array

dic_nonlinearities = {'exp': lambda x: np.exp(x), 'log_exp': lambda x: np.log(1 + np.exp(x))}

class GLM:

    def __init__(self, u0=0, kappa=None, eta=None, non_linearity='exp', noise='poisson'):
        self.u0 = u0
        self.kappa= kappa
        self.eta = eta
        self.non_linearity = dic_nonlinearities[non_linearity]
        self.noise = noise

    @property
    def r0(self):
        return np.exp(self.u0)

    def copy(self):
        return self.__class__(u0=self.u0, eta=self.eta.copy())

    def save(self, path):
        params = dict(u0=self.u0, eta=self.eta)
        with open(path, "wb") as fit_file:
            pickle.dump(params, fit_file)

    @classmethod
    def load(cls, path):
        with open(path, "rb") as fit_file:
            params = pickle.load(fit_file)
        glm = cls(u0=params['u0'], eta=params['eta'])
        return glm

    def sample(self, t, stim=None, shape=None, full=False):

        dt = get_dt(t)
        
        stim_shape = () if stim is None else stim.shape[1:]
        trials_shape = () if shape is None else shape
        shape = (len(t), ) + stim_shape + trials_shape
            
        r = np.zeros(shape) * np.nan
        eta_conv = np.zeros(shape)
        mask_spikes = np.zeros(shape, dtype=bool)

        if self.kappa is not None and stim is not None:
            kappa_conv = self.kappa.convolve_continuous(t, stim)
            kappa_conv = np.concatenate((np.zeros((1,) + stim.shape[1:]), kappa_conv[:-1]), axis=0)
            u = self.u0 + kappa_conv
            for ii, dim in enumerate(trials_shape):
                kappa_conv = np.stack([kappa_conv] * dim, ii + stim.ndim)
                u = np.stack([u] * dim, ii + stim.ndim)
        else:
            kappa_conv = np.zeros(shape)
            u = np.ones(shape) * self.u0

        j = 0
        while j < len(t):

            u[j, ...] = u[j, ...] + eta_conv[j, ...]
            r[j, ...] = self.non_linearity(u[j, ...])
            p_spk = 1 - np.exp(-r[j, ...] * dt)
            
            rand = np.random.rand(*shape[1:])
            mask_spikes[j, ...] = p_spk > rand

            if self.eta is not None and np.any(mask_spikes[j, ...]) and j < len(t) - 1:
                eta_conv[j + 1:, mask_spikes[j, ...]] += self.eta.interpolate(t[j + 1:] - t[j + 1])[:, None]

            j += 1
            
#         u = kappa_conv + eta_conv + self.u0
        
        if full:
            return kappa_conv, eta_conv, u, r, mask_spikes
        else:
            return u, r, mask_spikes

    def sample_conditioned(self, t, mask_spikes, stim=None, full=False):

        shape = mask_spikes.shape
        dt = get_dt(t)
        arg_spikes = np.where(shift_array(mask_spikes, 1, fill_value=False))
        t_spikes = (t[arg_spikes[0]],) + arg_spikes[1:]
#         print(shape, len(t_spikes))
        
        if self.kappa is not None and stim is not None:
            assert shape[:stim.ndim] == stim.shape
            kappa_conv = np.concatenate((np.zeros((1,) + stim.shape[1:]), self.kappa.convolve_continuous(t, stim)[:-1]), axis=0)
            u = kappa_conv + self.u0
            for ii, dim in enumerate(shape[stim.ndim:]):
                kappa_conv = np.stack([kappa_conv] * dim, ii + stim.ndim)
                u = np.stack([u] * dim, ii + stim.ndim)
        else:
            kappa_conv = np.zeros(shape)
            u = np.ones(shape) * self.u0
            
        if self.eta is not None and len(t_spikes[0]) > 0:
#             eta_conv = self.eta.convolve_discrete(t, t_spikes, shape=shape[1:]) #TODO. check if 1: or not
            eta_conv = self.eta.convolve_discrete(t, t_spikes) #TODO. check if 1: or not
            u = u + eta_conv
        else:
            eta_conv = np.zeros(shape)

        r = self.non_linearity(u)

        if full:
            return kappa_conv, eta_conv, u, r
        else:
            return u, r

    def gh_log_likelihood(self, dt, X, mask_spikes):

        theta = self.get_params()
        # TODO. adapt for arbitrary number of dims in X
        # TODO. other nonlinearities
        u = np.einsum('tka,a->tk', X, theta)
        r = np.exp(u)

        if self.noise == 'poisson':
            log_likelihood = -(np.sum(u[mask_spikes]) - dt * np.sum(r))
            g_log_likelihood = -(np.sum(X[mask_spikes, :], axis=0) - dt * np.einsum('tka,tk->a', X, r))
            h_log_likelihood = -(-dt * np.einsum('tka,tk,tkb->ab', X, r, X))
        elif self.noise == 'bernoulli':
            r_s, r_ns = r[mask_spikes], r[~mask_spikes]
            X_s, X_ns = X[mask_spikes, :], X[~mask_spikes, :]
            exp_r_s = np.exp(r_s * dt)
            log_likelihood = -(np.sum(np.log(1 - np.exp(-r_s * dt))) - dt * np.sum(r_ns))
            g_log_likelihood = -(dt * np.matmul(X_s.T, r_s / (exp_r_s - 1)) - dt * np.matmul(X_ns.T, r_ns))
            h_log_likelihood = -(dt * np.matmul(X_s.T * r_s * (exp_r_s * (1 - r_s * dt) - 1) / (exp_r_s - 1)**2, X_s) - \
                                     dt * np.matmul(X_ns.T * r_ns, X_ns))
                
        return log_likelihood, g_log_likelihood, h_log_likelihood, None

    def gh_objective(self,  dt, X, mask_spikes):
        return self.gh_log_likelihood(dt, X, mask_spikes)
    
    def get_params(self):
        n_kappa = 0 if self.kappa is None else self.kappa.nbasis
        n_eta = 0 if self.eta is None else self.eta.nbasis
        theta = np.zeros(1 + n_kappa + n_eta)
        theta[0] = self.u0
        if self.kappa is not None:
            theta[1:1 + n_kappa] = self.kappa.coefs
        if self.eta is not None:
            theta[1 + n_kappa:] = self.eta.coefs
        return theta

    def likelihood_kwargs(self, t, mask_spikes, stim=None):

        n_kappa = 0 if self.kappa is None else self.kappa.nbasis
        n_eta = 0 if self.eta is None else self.eta.nbasis

        X = np.zeros(mask_spikes.shape + (1 + n_kappa + n_eta,))
        X[..., 0] = 1

        if self.kappa is not None and stim is not None:
            n_kappa = self.kappa.nbasis
            X_kappa = self.kappa.convolve_basis_continuous(t, stim)
            X_kappa = np.concatenate((np.zeros((1,) + X_kappa.shape[1:]), X_kappa[:-1]), axis=0)
            for ii, dim in enumerate(mask_spikes.shape[stim.ndim:]):
                X_kappa = np.stack([X_kappa] * dim, ii + stim.ndim)
            X[..., 1:1 + n_kappa] = X_kappa
        
        if self.eta is not None:
            args = np.where(shift_array(mask_spikes, 1, fill_value=False))
            t_spk = (t[args[0]],) + args[1:]
            n_eta = self.eta.nbasis
            X_eta = self.eta.convolve_basis_discrete(t, t_spk, shape=mask_spikes.shape)
            X[..., 1 + n_kappa:] = X_eta

        likelihood_kwargs = dict(dt=get_dt(t), X=X, mask_spikes=mask_spikes)

        return likelihood_kwargs

    def objective_kwargs(self, t, mask_spikes, stim=None):
        return self.likelihood_kwargs(t=t, mask_spikes=mask_spikes, stim=stim)

    def set_params(self, theta):
        n_kappa = 0 if self.kappa is None else self.kappa.nbasis
        self.u0 = theta[0]
        if self.kappa is not None:
            self.kappa.coefs = theta[1:1 + n_kappa]
        if self.eta is not None:
            self.eta.coefs = theta[1 + n_kappa:]
        return self

    def fit(self, t, mask_spikes, stim=None, newton_kwargs=None, verbose=False):

        newton_kwargs = {} if newton_kwargs is None else newton_kwargs

        objective_kwargs = self.objective_kwargs(t, mask_spikes, stim=stim)
        gh_objective = partial(self.gh_objective, **objective_kwargs)

        optimizer = NewtonMethod(model=self, gh_objective=gh_objective, verbose=verbose, **newton_kwargs)
        optimizer.optimize()

        return optimizer
