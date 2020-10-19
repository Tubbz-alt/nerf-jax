#!/usr/bin/env python3

import numpy as np
import jax
import jax.numpy as jnp
from jax import jit, vmap
import functools

from nerf_helpers import positional_encoding, map_batched, map_batched_rng, sample_pdf
from volume_render import volume_render_radiance_field


@functools.partial(jit, static_argnums=(0, 3, 4, 5))
def run_network(
    network_fn,
    pts,
    ray_batch,
    chunksize,
    xyz_encoding_functions,
    view_encoding_functions,
):
    """
    >>> import haiku as hk
    >>> from models import FlexibleNeRFModel
    >>> from torch_to_jax import torch_to_jax

    >>> x = np.random.randn(250, 66).astype(np.float32)

    >>> net_torch = FlexibleNeRFModelTorch()
    >>> net_jax = hk.without_apply_rng(hk.transform(jax.jit(lambda x: FlexibleNeRFModel()(x))))

    >>> jax_params = torch_to_jax(dict(net_torch.named_parameters()), 'flexible_ne_rf_model')

    >>> jax_out = net_jax.apply(jax_params, jnp.array(x))
    >>> torch_out = net_torch(torch.from_numpy(x))

    >>> pts_np = np.random.random((256, 128, 3)).astype(np.float32)
    >>> ray_batch_np = np.random.random((256, 11)).astype(np.float32)

    >>> pts_torch = torch.from_numpy(pts_np)
    >>> ray_batch_torch = torch.from_numpy(ray_batch_np)

    >>> pts_jax = jnp.array(pts_np)
    >>> ray_batch_jax = jnp.array(ray_batch_np)

    >>> torch_result = run_network_torch(net_torch, pts_torch, ray_batch_torch, 32,
    ...                                 lambda p: positional_encoding_torch(p, 6),
    ...                                 lambda p: positional_encoding_torch(p, 4))
    >>> jax_result = run_network(functools.partial(net_jax.apply, jax_params), pts_jax, ray_batch_jax, 32, 6, 4)

    >>> np.allclose(np.array(jax_result), torch_result.detach().numpy(), atol=1e-7)
    True
    """
    pts_flat = pts.reshape((-1, pts.shape[-1]))

    embedded = vmap(lambda x: positional_encoding(x, xyz_encoding_functions))(pts_flat)

    viewdirs = ray_batch[..., None, -3:]
    input_dirs = jnp.broadcast_to(viewdirs, pts.shape)
    input_dirs_flat = input_dirs.reshape((-1, input_dirs.shape[-1]))

    embedded_dirs = vmap(lambda x: positional_encoding(x, view_encoding_functions))(
        input_dirs_flat
    )
    embedded = jnp.concatenate((embedded, embedded_dirs), axis=-1)

    radiance_field = map_batched(embedded, network_fn, chunksize, False)

    radiance_field = radiance_field.reshape(
        list(pts.shape[:-1]) + [radiance_field.shape[-1]]
    )

    return radiance_field


@functools.partial(jax.jit, static_argnums=(1, 2, 3, 4))
def predict_and_render_radiance(
    ray_batch, model_coarse, model_fine, options, model_options, rng
):
    num_rays = ray_batch.shape[0]
    ro, rd = ray_batch[..., :3], ray_batch[..., 3:6]
    bounds = ray_batch[..., 6:8].reshape((-1, 1, 2))
    near, far = bounds[..., 0], bounds[..., 1]

    t_vals = jnp.linspace(0.0, 1.0, options.num_coarse, dtype=ro.dtype,)
    if not options.lindisp:
        z_vals = near * (1.0 - t_vals) + far * t_vals
    else:
        z_vals = 1.0 / (1.0 / near * (1.0 - t_vals) + 1.0 / far * t_vals)
    z_vals = jnp.broadcast_to(z_vals, ([num_rays, options.num_coarse]))

    if options.perturb:
        mids = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
        upper = jnp.concatenate((mids, z_vals[..., -1:]), axis=-1)
        lower = jnp.concatenate((z_vals[..., :1], mids), axis=-1)

        rng, subrng = jax.random.split(rng)
        t_rand = jax.random.uniform(subrng, z_vals.shape)
        z_vals = lower + (upper - lower) * t_rand

    # pts -> (num_rays, N_samples, 3)
    pts = ro[..., None, :] + rd[..., None, :] * z_vals[..., :, None]

    radiance_field = run_network(
        model_coarse,
        pts,
        ray_batch,
        options.chunksize,
        model_options.coarse.num_encoding_fn_xyz,
        model_options.coarse.num_encoding_fn_dir,
    )

    rng, subrng = jax.random.split(rng)
    (
        rgb_coarse,
        disp_coarse,
        acc_coarse,
        weights,
        depth_coarse,
    ) = volume_render_radiance_field(
        radiance_field,
        z_vals,
        rd,
        subrng,
        options.radiance_field_noise_std,
        options.white_background,
    )

    if options.num_fine > 0:
        rng, subrng = jax.random.split(rng)
        z_vals_mid = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
        z_samples = sample_pdf(
            z_vals_mid,
            weights[..., 1:-1],
            options.num_fine,
            subrng,
            (options.perturb == 0.0),
        )
        jax.lax.stop_gradient(z_samples)

        z_vals = jax.lax.sort(
            jnp.concatenate((z_vals, z_samples), axis=-1), dimension=-1
        )
        # pts -> (N_rays, N_samples + N_importance, 3)
        pts = ro[..., None, :] + rd[..., None, :] * z_vals[..., :, None]

        radiance_field = run_network(
            model_fine,
            pts,
            ray_batch,
            options.chunksize,
            model_options.fine.num_encoding_fn_xyz,
            model_options.fine.num_encoding_fn_dir,
        )
        rng, subrng = jax.random.split(rng)
        rgb_fine, disp_fine, acc_fine, _, _ = volume_render_radiance_field(
            radiance_field,
            z_vals,
            rd,
            subrng,
            options.radiance_field_noise_std,
            options.white_background,
        )

        return (
            rgb_coarse,
            disp_coarse[..., jnp.newaxis],
            acc_coarse[..., jnp.newaxis],
            rgb_fine,
            disp_fine[..., jnp.newaxis],
            acc_fine[..., jnp.newaxis],
        )
    else:
        return (
            rgb_coarse,
            disp_coarse[..., jnp.newaxis],
            acc_coarse[..., jnp.newaxis],
            None,
            None,
            None,
        )


@functools.partial(jax.jit, static_argnums=(3, 4, 7, 8, 9, 11))
def run_one_iter_of_nerf(
    height,
    width,
    focal_length,
    model_coarse,
    model_fine,
    ray_origins,
    ray_directions,
    options,
    model_options,
    dataset_options,
    rng,
    validation,
):
    if options.use_viewdirs:
        # Provide ray directions as input
        viewdirs = ray_directions
        viewdirs = (
            viewdirs / jnp.linalg.norm(viewdirs, ord=2, axis=-1)[..., jnp.newaxis]
        )
        viewdirs = viewdirs.reshape((-1, 3))

    if dataset_options.no_ndc is False:
        ro, rd = ndc_rays(height, width, focal_length, 1.0, ray_origins, ray_directions)
        ro = ro.reshape((-1, 3))
        rd = rd.reshape((-1, 3))
    else:
        ro = ray_origins.reshape((-1, 3))
        rd = ray_directions.reshape((-1, 3))

    near = dataset_options.near * jnp.ones_like(rd[..., :1])
    far = dataset_options.far * jnp.ones_like(rd[..., :1])
    rays = jnp.concatenate((ro, rd, near, far), axis=-1)
    if options.use_viewdirs:
        rays = jnp.concatenate((rays, viewdirs), axis=-1)

    render_rays = lambda batch_rng: jnp.concatenate(
        tuple(
            pred
            for pred in predict_and_render_radiance(
                batch_rng[0],
                model_coarse,
                model_fine,
                options,
                model_options,
                batch_rng[1],
            )
            if pred is not None
        ),
        axis=-1,
    )

    images, rng = map_batched_rng(rays, render_rays, options.chunksize, False, rng)

    if validation:
        return rng, images.reshape(ray_directions.shape[:-1] + (10,))
    else:
        return rng, images


if __name__ == "__main__":
    import doctest
    import torch
    from torch_impl import *

    print(doctest.testmod(exclude_empty=True))
