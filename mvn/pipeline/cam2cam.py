import os
import numpy as np

import torch

from mvn.models.utils import get_grad_params
from mvn.pipeline.utils import get_kp_gt, backprop
from mvn.utils.misc import live_debug_log
from mvn.utils.multiview import triangulate_batch_of_points_in_cam_space,homogeneous_to_euclidean, euclidean_to_homogeneous, prepare_weak_cams_for_dlt
from mvn.models.loss import GeodesicLoss, KeypointsMSELoss, MSESmoothLoss, KeypointsMSESmoothLoss, ProjectionLoss, ScaleDependentProjectionLoss, PseudoHuberLoss, BerHuLoss, BodyLoss
from mvn.utils.tred import apply_umeyama

_ITER_TAG = 'cam2cam'
PELVIS_I = 6


def center2pelvis(keypoints_2d, pelvis_i=PELVIS_I):
    """ pelvis -> (0, 0) """

    n_joints = keypoints_2d.shape[2]
    pelvis_point = keypoints_2d[:, :, pelvis_i, :]

    return keypoints_2d - pelvis_point.unsqueeze(2).repeat(1, 1, n_joints, 1)  # in each view: joint coords - pelvis coords


def dist2pelvis(keypoints_2d_in_view, pelvis_i=PELVIS_I):
    return torch.mean(torch.cat([
        torch.norm(
            keypoints_2d_in_view[i] -\
            keypoints_2d_in_view[pelvis_i]
        ).unsqueeze(0)
        for i in range(keypoints_2d_in_view.shape[0])
        if i != pelvis_i
    ])).unsqueeze(0)


def normalize_keypoints(keypoints_2d, pelvis_center_kps, normalization):
    """ "divided by its Frobenius norm in the preprocessing" """

    batch_size, n_views = keypoints_2d.shape[0], keypoints_2d.shape[1]

    if pelvis_center_kps:
        kps = center2pelvis(keypoints_2d)
    else:
        kps = keypoints_2d

    if normalization == 'd2pelvis':
        scaling = torch.cat([
            torch.max(
                torch.cat([
                    dist2pelvis(kps[batch_i, view_i])
                    for view_i in range(n_views)
                ]).unsqueeze(0)
            ).unsqueeze(0).repeat(1, n_views)  # same for each view
            for batch_i in range(batch_size)
        ])
    elif normalization == 'fro':
        scaling = torch.cat([
            torch.cat([
                torch.norm(kps[batch_i, view_i], p='fro').unsqueeze(0)
                for view_i in range(n_views)
            ]).unsqueeze(0)
            for batch_i in range(batch_size)
        ])
    elif normalization == 'maxfro':
        scaling = torch.cat([
            torch.max(
                torch.cat([
                    torch.norm(kps[batch_i, view_i], p='fro').unsqueeze(0)
                    for view_i in range(n_views)
                ]).unsqueeze(0)
            ).unsqueeze(0).repeat(1, n_views)  # same for each view
            for batch_i in range(batch_size)
        ])
    elif normalization == 'fixed':
        factor = 40.0  # todo to be scaled with K
        scaling = factor * torch.ones(batch_size, n_views)

    return torch.cat([
        torch.cat([
            (
                kps[batch_i, view_i] / scaling[batch_i, view_i]
            ).unsqueeze(0)
            for view_i in range(n_views)
        ]).unsqueeze(0)
        for batch_i in range(batch_size)
    ])


def _get_cams_gt(cameras, where='world'):
    """ master is 0 """

    n_cameras = len(cameras)
    batch_size = len(cameras[0])

    cam_gts = torch.zeros(batch_size, n_cameras, 4, 4)
    for batch_i in range(batch_size):
        if where == 'world':
            cam_gts[batch_i] = torch.cat([
                torch.tensor(
                    cameras[i][batch_i].extrinsics_padded
                ).unsqueeze(0)
                for i in range(n_cameras)
            ])
        elif where == 'master':
            master = torch.tensor(cameras[0][batch_i].extrinsics_padded)
            from_master = torch.inverse(master)

            cam_gts[batch_i, 0] = master.clone()
            cam_gts[batch_i, 1:] = torch.cat([
                torch.mm(
                    torch.tensor(cameras[i][batch_i].extrinsics_padded),
                    from_master.clone()
                ).unsqueeze(0)
                for i in range(1, n_cameras)
            ])

    return cam_gts.cuda()


def _forward_cams(cam2cam_model, detections, gt, config):
    preds = cam2cam_model(
        detections
    )  # (batch_size, ~ |views|, 4, 4)
    dev = preds.device

    if config.cam2cam.cams.using_just_one_gt:
        preds = torch.cat([
            gt[:, 0].unsqueeze(1),
            preds[:, 1:]
        ], dim=1)

    if config.cam2cam.cams.using_gt.really:
        preds = gt

        noisy = config.cam2cam.cams.using_gt.using_noise
        if noisy > 0.0:
            preds[:, :, :3, :3] += 1e-2 * noisy *\
                torch.rand_like(preds[:, :, :3, :3])
            preds[:, :, :3, 3] += 1e2 * noisy *\
                torch.rand_like(preds[:, :, :3, 3])

    return preds.to(dev)


def triangulate(cams, keypoints_2d_pred, confidences_pred, K, master_cam_i, where="world"):
    full_cams = prepare_weak_cams_for_dlt(
        cams,
        K.to(cams.device).type(torch.get_default_dtype()),
        where
    )

    # ... perform DLT in master cam space ...
    kps_pred = triangulate_batch_of_points_in_cam_space(
        full_cams.cpu(),
        keypoints_2d_pred.cpu(),
        confidences_batch=confidences_pred.cpu()
    ).to(keypoints_2d_pred.device)

    if where == 'world':
        return None, kps_pred
    elif where == 'master':  # ... but since they're in master cam space ...
        batch_size = cams.shape[0]
        kps_world_pred = torch.cat([
            homogeneous_to_euclidean(
                euclidean_to_homogeneous(
                    kps_pred[batch_i]
                ).to(cams.device)
                @
                torch.inverse(
                    cams[batch_i, master_cam_i].T
                )
            ).unsqueeze(0)
            for batch_i in range(batch_size)
        ])
        return kps_pred, kps_world_pred


def _compute_losses(cam_preds, cam_gts, confidences_pred, keypoints_2d_pred, kps_mastercam_pred, kps_world_pred, kps_world_gt, keypoints_3d_binary_validity_gt, cameras, config):
    dev = cam_preds.device
    total_loss = torch.tensor(0.0).to(dev)  # real loss, the one grad is applied to
    batch_size = cam_gts.shape[0]
    n_cameras = cam_gts.shape[1]
    start_cam = 1 if config.cam2cam.cams.using_just_one_gt else 0
    loss_weights = config.cam2cam.loss  # todo normalize | sum = 1

    just_R = lambda x: x[:, start_cam:n_cameras].reshape(-1, 4, 4)[:, :3, :3]
    loss_R = GeodesicLoss()(
        just_R(cam_gts),
        just_R(cam_preds)
    )
    if loss_weights.R > 0:
        total_loss += loss_weights.R * loss_R

    just_t = lambda x: x[:, start_cam:n_cameras].reshape(-1, 4, 4)[:, :3, 3]
    t_loss = MSESmoothLoss(threshold=4e2)(
        just_t(cam_gts) / config.cam2cam.postprocess.scale_t,
        just_t(cam_preds) / config.cam2cam.postprocess.scale_t
    )
    if loss_weights.t > 0:
        total_loss += loss_weights.t * t_loss

    K = torch.tensor(cameras[0][0].intrinsics_padded)  # same for all
    loss_proj = ProjectionLoss(
        criterion=KeypointsMSESmoothLoss(threshold=2.0),  # HuberLoss(threshold=1e-1),
        where=config.cam2cam.triangulate
    )(
        K,
        cam_preds[:, start_cam:],
        kps_mastercam_pred if config.cam2cam.triangulate == 'master' else kps_world_pred,
        keypoints_2d_pred[:, start_cam:],  # todo just because I'm using GT KPs
    )
    if loss_weights.proj > 0:
        total_loss += loss_weights.proj * loss_proj

    loss_world = KeypointsMSESmoothLoss(threshold=20*20)(
        kps_world_pred * config.opt.scale_keypoints_3d,
        kps_world_gt.to(dev) * config.opt.scale_keypoints_3d,
        keypoints_3d_binary_validity_gt,
    )
    if loss_weights.world > 0:
        total_loss += loss_world * loss_weights.world

    if config.cam2cam.triangulate == 'master':
        extrinsics = torch.cat([
            torch.cat([
                torch.mm(
                    cam_preds[batch_i, i],  # master2i = i * master^-1
                    cam_preds[batch_i, 0]  # master
                ).unsqueeze(0) if i > 0 else cam_preds[batch_i, 0].unsqueeze(0)
                for i in range(n_cameras)
            ]).unsqueeze(0)
            for batch_i in range(batch_size)
        ])
        _, kps_world_pred_from_exts = triangulate(
            extrinsics, keypoints_2d_pred, confidences_pred, K, 0, 'world'
        )
        loss_self_world = KeypointsMSESmoothLoss(threshold=20*20)(
            kps_world_pred * config.opt.scale_keypoints_3d,
            kps_world_pred_from_exts * config.opt.scale_keypoints_3d,
            keypoints_3d_binary_validity_gt,
        )
    elif config.cam2cam.triangulate == 'world':
        loss_self_world = torch.tensor(0.0)
    if loss_weights.self_consistency.world > 0:
        total_loss += loss_self_world * loss_weights.self_consistency.world

    loss_self_proj = ScaleDependentProjectionLoss(
        criterion=BerHuLoss(threshold=0.25),
        where=config.cam2cam.triangulate
    )(
        K,
        cam_preds,
        kps_mastercam_pred if config.cam2cam.triangulate == 'master' else kps_world_pred,
        keypoints_2d_pred
    )
    if loss_weights.self_consistency.proj > 0:
        total_loss += loss_self_proj * loss_weights.self_consistency.proj

    loss_body = BodyLoss(
        criterion=BerHuLoss(threshold=10.0)  # or even `20.0`
    )(kps_world_pred, kps_world_gt)
    if loss_weights.body > 0:
        total_loss += loss_body * loss_weights.body

    if config.debug.show_live:
        __batch_i = 0

        print('pred exts {:.0f}'.format(__batch_i))
        print(cam_preds[__batch_i, start_cam:n_cameras, :3, :4])
        print('gt exts {:.0f}'.format(__batch_i))
        print(cam_gts[__batch_i, start_cam:n_cameras, :3, :4])

        print('pred batch {:.0f}'.format(__batch_i))
        print(kps_world_pred[__batch_i])
        print('gt batch {:.0f}'.format(__batch_i))
        print(kps_world_gt[__batch_i])

    return loss_R, t_loss,\
        loss_proj, loss_world,\
        loss_self_world, loss_self_proj, loss_body,\
        total_loss


def batch_iter(epoch_i, indices, cameras, iter_i, model, cam2cam_model, opt, scheduler, images_batch, kps_world_gt, keypoints_3d_binary_validity_gt, is_train, config, minimon, experiment_dir):
    iter_folder = 'epoch-{:.0f}-iter-{:.0f}'.format(epoch_i, iter_i)
    iter_dir = os.path.join(experiment_dir, iter_folder) if experiment_dir else None

    def _save_stuff(stuff, var_name):
        if iter_dir:
            os.makedirs(iter_dir, exist_ok=True)
            f_out = os.path.join(iter_dir, var_name + '.trc')
            torch.save(torch.tensor(stuff), f_out)

    def _forward_kp():
        if config.cam2cam.data.using_gt:
            keypoints_2d_pred, heatmaps_pred, confidences_pred = get_kp_gt(
                kps_world_gt,
                cameras,
                config.cam2cam.cams.use_extra_cams,
                config.cam2cam.data.using_noise
            )
        else:
            keypoints_2d_pred, heatmaps_pred, confidences_pred = model(
                images_batch, None, minimon
            )

        return keypoints_2d_pred, heatmaps_pred, confidences_pred

    def _backprop():
        minimon.enter()
        loss_R, t_loss, loss_2d, loss_3d, loss_self_world, loss_self_proj, loss_body, total_loss = _compute_losses(
            cam_preds,
            cam_gts,
            confidences_pred,
            keypoints_2d_pred,
            kps_mastercam_pred,
            kps_world_pred,
            kps_world_gt,
            keypoints_3d_binary_validity_gt,
            cameras,
            config,
        )
        minimon.leave('compute loss')

        message = '{} batch iter {:d} losses: R ~ {:.1f}, t ~ {:.2f}, PROJ ~ {:.0f}, WORLD ~ {:.0f}, SELF WORLD ~ {:.0f}, SELF PROJ ~ {:.3f}, BODY ~ {:.3f}, TOTAL ~ {:.0f}'.format(
            'training' if is_train else 'validation',
            iter_i,
            loss_R.item(),
            t_loss.item(),
            loss_2d.item(),
            loss_3d.item(),
            loss_self_world.item(),
            loss_self_proj.item(),
            loss_body.item(),
            total_loss.item(),
        )
        live_debug_log(_ITER_TAG, message)

        minimon.enter()

        current_lr = opt.param_groups[0]['lr']
        clip = config.cam2cam.opt.grad_clip / current_lr

        backprop(
            opt, total_loss, scheduler,
            loss_self_proj,
            _ITER_TAG, get_grad_params(cam2cam_model), clip
        )
        minimon.leave('backward pass')

    minimon.enter()
    keypoints_2d_pred, _, confidences_pred = _forward_kp()
    if config.debug.dump_tensors:
        _save_stuff(keypoints_2d_pred, 'keypoints_2d_pred')
    minimon.leave('BB forward')

    cam_gts = _get_cams_gt(
        cameras,
        config.cam2cam.triangulate
    )
    detections = normalize_keypoints(
        keypoints_2d_pred,
        config.cam2cam.preprocess.pelvis_center_kps,
        config.cam2cam.preprocess.normalize_kps
    ).to('cuda:0').type(torch.get_default_dtype())  # todo device

    minimon.enter()
    master_i = 0  # views are randomly sorted => no need for a random master within batch
    cam_preds = _forward_cams(
        cam2cam_model,
        detections,
        cam_gts,
        config,
    )
    if config.debug.dump_tensors:
        _save_stuff(cam_preds, 'cam_preds')
    minimon.leave('forward')

    minimon.enter()
    kps_mastercam_pred, kps_world_pred = triangulate(
        cam_preds,
        keypoints_2d_pred,
        confidences_pred,
        torch.tensor(cameras[0][0].intrinsics_padded).to(cam_preds.device),
        master_i,
        where=config.cam2cam.triangulate
    )

    if config.debug.dump_tensors:
        _save_stuff(kps_world_pred, 'kps_world_pred')
        _save_stuff(indices, 'batch_indexes')
        _save_stuff(kps_world_gt, 'kps_world_gt')

    minimon.leave('triangulate')

    if is_train:
        _backprop()

    if config.cam2cam.postprocess.force_pelvis_in_origin:
        kps_world_pred = torch.cat([
            torch.cat([
                kps_world_pred[batch_i] -\
                    kps_world_pred[batch_i, PELVIS_I].unsqueeze(0).repeat(17, 1)
            ]).unsqueeze(0)
            for batch_i in range(kps_world_pred.shape[0])
        ])

    if config.cam2cam.postprocess.try2align:
        kps_world_pred = apply_umeyama(
            kps_world_gt.to(kps_world_pred.device).type(torch.get_default_dtype()),
            kps_world_pred,
            scaling=config.cam2cam.postprocess.try2scale
        )

    return kps_world_pred.detach().cpu()  # no need for grad no more
