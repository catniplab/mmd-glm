import numpy as np
import torch

from .base import GLM
from ..utils import get_dt, shift_array


class MBMMDGLM(GLM, torch.nn.Module):

    def __init__(self, u0=0, kappa=None, eta=None, non_linearity='exp'):
        torch.nn.Module.__init__(self)
        GLM.__init__(self, u0=u0, kappa=kappa, eta=eta, non_linearity=non_linearity)
        
        n_kappa = 0 if self.kappa is None else self.kappa.nbasis
        n_eta = 0 if self.eta is None else self.eta.nbasis

        b = torch.tensor([u0]).double()
        self.register_parameter("b", torch.nn.Parameter(b))
        
        if self.kappa is not None:
            kappa_coefs = torch.from_numpy(kappa.coefs)
            self.register_parameter("kappa_coefs", torch.nn.Parameter(kappa_coefs))
        if self.eta is not None:
            eta_coefs = torch.from_numpy(eta.coefs)
            self.register_parameter("eta_coefs", torch.nn.Parameter(eta_coefs))
            
    def forward(self, t, stim=None, n_batch_fr=None):
        
        dt = get_dt(t)
        theta_g = self.get_params()
        
        if stim is not None:
            _, _, mask_spikes_fr = self.sample(t, stim=stim)
        else:
            _, _, mask_spikes_fr = self.sample(t, shape=(n_batch_fr,))

        X_fr = torch.from_numpy(self.objective_kwargs(t, mask_spikes_fr, stim=stim)['X'])
        u_fr = torch.einsum('tka,a->tk', X_fr, theta_g)
        r_fr = torch.exp(u_fr)
        mask_spikes_fr = torch.from_numpy(mask_spikes_fr)
        
        return r_fr, mask_spikes_fr, X_fr
    
    def get_params(self):
        n_kappa = 0 if self.kappa is None else self.kappa.nbasis
        n_eta = 0 if self.eta is None else self.eta.nbasis
        theta = torch.zeros(1 + n_kappa + n_eta)
        theta[0] = self.b
        if self.kappa is not None:
            theta[1:1 + n_kappa] = self.kappa_coefs
        if self.eta is not None:
            theta[1 + n_kappa:] = self.eta_coefs
        theta = theta.double()
        return theta
    
    def _log_likelihood(self, dt, mask_spikes, X_dc):
        theta_g = self.get_params()
        u_dc = torch.einsum('tka,a->tk', X_dc, theta_g)
        r_dc = torch.exp(u_dc)
        neg_log_likelihood = -(torch.sum(torch.log(1 - torch.exp(-dt * r_dc) + 1e-24) * mask_spikes.double()) - \
                               dt * torch.sum(r_dc * (1 - mask_spikes.double())))
        return neg_log_likelihood
    
    def train(self, t, mask_spikes, phi=None, kernel=None, stim=None, log_likelihood=False, lam_mmd=1e0, biased=False, 
              optim=None, clip=None, num_epochs=20, n_batch_fr=100, kernel_kwargs=None, verbose=False, metrics=None, 
              n_metrics=25):

        n_d = mask_spikes.shape[1]
    
        dt = torch.tensor([get_dt(t)])
        loss, nll = [], []
        
        X_dc = torch.from_numpy(self.objective_kwargs(t, mask_spikes, stim=stim)['X']).double()
        
        kernel_kwargs = kernel_kwargs if kernel_kwargs is not None else {}
        
        if phi is None:
            idx_fr = np.triu_indices(n_batch_fr, k=1)
            idx_fr = (torch.from_numpy(idx_fr[0]), torch.from_numpy(idx_fr[1]))
            idx_d = np.triu_indices(n_d, k=1)
            idx_d = (torch.from_numpy(idx_d[0]), torch.from_numpy(idx_d[1]))
                
        _loss = torch.tensor([np.nan])

        for epoch in range(num_epochs):
            if verbose:
                print('\r', 'epoch', epoch, 'of', num_epochs, 
                      'loss', np.round(_loss.item(), 10), end='')
            
            optim.zero_grad()
            
            theta_g = self.get_params()
            u_dc = torch.einsum('tka,a->tk', X_dc, theta_g)
            r_dc = torch.exp(u_dc)
            
            r_fr, mask_spikes_fr, X_fr = self(t, stim=stim, n_batch_fr=n_batch_fr)

            if phi is not None:
                phi_d = phi(t, r_dc, model=self, **kernel_kwargs)
                phi_fr = phi(t, r_fr, model=self, **kernel_kwargs)
                
                if not biased:
                    sum_phi_d = torch.sum(phi_d, 1)
                    sum_phi_fr = torch.sum(phi_fr, 1)

                    norm2_d = (torch.sum(sum_phi_d**2) - torch.sum(phi_d**2)) / (n_d * (n_d - 1))
                    norm2_fr = (torch.sum(sum_phi_fr**2) - torch.sum(phi_fr**2)) / (n_batch_fr * (n_batch_fr - 1))
                    mean_dot = torch.sum(sum_phi_d * sum_phi_fr) / (n_d * n_batch_fr)
                        
                    mmd_grad = norm2_d + norm2_fr - 2 * mean_dot
                else:
                    mmd_grad = torch.sum((torch.mean(phi_d, 1) - torch.mean(phi_fr, 1))**2)
            else:
                gramian_d_d = kernel(t, r_dc, r_dc, model=self)
                gramian_fr_fr = kernel(t, r_fr, r_fr, model=self)
                gramian_d_fr = kernel(t, r_dc, r_fr, model=self)
                if not biased:
                    mmd_grad = torch.mean(gramian_d_d[idx_d]) + torch.mean(gramian_fr_fr[idx_fr]) \
                                -2 * torch.mean(gramian_d_fr)
                else:
                    mmd_grad = torch.mean(gramian_d_d) + torch.mean(gramian_fr_fr) \
                                -2 * torch.mean(gramian_d_fr)
            
            _loss = lam_mmd * mmd_grad
            
            if log_likelihood:
                _nll = self._log_likelihood(dt, mask_spikes, X_dc)
                nll.append(_nll.item())
                _loss = _loss + _nll

            _loss.backward()
            if clip is not None:
                torch.nn.utils.clip_grad_value_(self.parameters(), clip)
                
            if (epoch % n_metrics) == 0:
                
                _metrics = metrics(self, t, mask_spikes, mask_spikes_fr) if metrics is not None else {}

                if phi is not None:
                    _metrics['mmd'] = (torch.sum((torch.mean(phi_d.detach(), 1) - torch.mean(phi_fr.detach(), 1))**2)).item()
                else:
                    _metrics['mmd'] = torch.mean(gramian_d_d.detach()[idx_d]) + torch.mean(gramian_fr_fr.detach()[idx_fr]) \
                                      - 2 * torch.mean(gramian_d_fr.detach())
                
                if epoch == 0:
                    metrics_list = {key:[val] for key, val in _metrics.items()}
                else:
                    for key, val in _metrics.items():
                        metrics_list[key].append(val)
            
            optim.step()
            
            theta_g = self.get_params()
            self.set_params(theta_g.data.detach().numpy())
            
            loss.append(_loss.item())
            
        if metrics is None:
            metrics_list = None
        
        return loss, nll, metrics_list

