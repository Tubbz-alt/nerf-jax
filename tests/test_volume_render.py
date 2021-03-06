import pytest

import numpy as np
import jax
import jax.numpy as jnp
from jax import jit, grad

import torch

from reference import volume_render_radiance_field_torch
from nerf import volume_render_radiance_field


def test_sigmoid():
    data = np.random.random((200,))
    a = torch.sigmoid(torch.from_numpy(data))
    b = jax.nn.sigmoid(jnp.array(data))

    assert np.allclose(a.numpy(), np.array(b))


def test_volume_render_radiance_field():
    rng = jax.random.PRNGKey(1010)

    raw_np = np.random.uniform(size=(2, 2, 8, 4)).astype(np.float32)
    rays_o_np = np.random.uniform(size=(2, 2, 3)).astype(np.float32)
    #rays_o_np = np.zeros((2, 2, 3), dtype=np.float32)
    z_vals_np = np.random.uniform(size=(8)).astype(np.float32)

    raw_torch = torch.from_numpy(raw_np)
    rays_o_torch = torch.from_numpy(rays_o_np)
    z_vals_torch = torch.from_numpy(z_vals_np)

    raw_jax = jnp.array(raw_np)
    rays_o_jax = jnp.array(rays_o_np)
    z_vals_jax = jnp.array(z_vals_np)

    (
        rgb_torch,
        disp_torch,
        acc_torch,
        weights_torch,
        depth_torch,
    ) = volume_render_radiance_field_torch(raw_torch, z_vals_torch, rays_o_torch)

    for i in range(5):
        rgb, disp, acc, weights, depth = volume_render_radiance_field(
            raw_jax, z_vals_jax, rays_o_jax, rng, 0.0, False
        )

        loss_fn = lambda *args: volume_render_radiance_field(*args)[i].flatten().sum()

        volume_grad_fn = jit(grad(loss_fn, argnums=(0, 1, 2)), static_argnums=(4, 5),)
        volume_grad = volume_grad_fn(raw_jax, z_vals_jax, rays_o_jax, rng, 0.0, False)

        assert not any(jnp.isnan(dg.sum()) for dg in volume_grad)

    assert np.allclose(rgb_torch.numpy(), np.array(rgb))
    assert np.allclose(disp_torch.numpy(), np.array(disp))
    assert np.allclose(acc_torch.numpy(), np.array(acc))
    assert np.allclose(weights_torch.numpy(), np.array(weights))
    assert np.allclose(depth_torch.numpy(), np.array(depth))
