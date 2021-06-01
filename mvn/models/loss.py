import numpy as np

import torch
from torch import nn
from torch.functional import norm

from mvn.utils.multiview import project_to_weak_views
from mvn.utils.tred import get_cam_location_in_world


class KeypointsMSELoss(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, keypoints_pred, keypoints_gt, keypoints_binary_validity):
        dimension = keypoints_pred.shape[-1]
        loss = torch.sum((keypoints_gt - keypoints_pred) ** 2 * keypoints_binary_validity)
        loss = loss / (dimension * max(1, torch.sum(keypoints_binary_validity).item()))
        return loss


class KeypointsMSESmoothLoss(nn.Module):
    def __init__(self, threshold=20*20, alpha=0.1, beta=0.9):
        super().__init__()

        self.threshold = threshold
        self.alpha = alpha
        self.beta = beta

    def forward(self, keypoints_pred, keypoints_gt, keypoints_binary_validity=None):
        dimension = keypoints_pred.shape[-1]
        diff = (keypoints_gt - keypoints_pred) ** 2

        if not (keypoints_binary_validity is None):
            diff *= keypoints_binary_validity

        diff[diff > self.threshold] = torch.pow(
            diff[diff > self.threshold], self.alpha
        ) * (self.threshold ** self.beta)  # soft version
        loss = torch.sum(diff) / dimension
        
        if not (keypoints_binary_validity is None):
            loss /= max(1, torch.sum(keypoints_binary_validity).item())

        return loss


class KeypointsMAELoss(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(x):
        return None  # todo


class MSESmoothLoss(nn.Module):
    def __init__(self, threshold, alpha=0.1, beta=0.9):
        super().__init__()

        self.threshold = threshold

    def forward(self, pred, gt):
        diff = (gt - pred) ** 2

        diff[diff > self.threshold] = torch.pow(diff[diff > self.threshold], 0.1) * (self.threshold ** 0.9)  # soft version
        return torch.mean(diff)


class VolumetricCELoss(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, coord_volumes_batch, volumes_batch_pred, keypoints_gt, keypoints_binary_validity):
        loss = 0.0
        n_losses = 0

        batch_size = volumes_batch_pred.shape[0]
        for batch_i in range(batch_size):
            coord_volume = coord_volumes_batch[batch_i]
            keypoints_gt_i = keypoints_gt[batch_i]

            coord_volume_unsq = coord_volume.unsqueeze(0)
            keypoints_gt_i_unsq = keypoints_gt_i.unsqueeze(1).unsqueeze(1).unsqueeze(1)

            dists = torch.sqrt(((coord_volume_unsq - keypoints_gt_i_unsq) ** 2).sum(-1))
            dists = dists.view(dists.shape[0], -1)

            min_indexes = torch.argmin(dists, dim=-1).detach().cpu().numpy()
            min_indexes = np.stack(np.unravel_index(min_indexes, volumes_batch_pred.shape[-3:]), axis=1)

            for joint_i, index in enumerate(min_indexes):
                validity = keypoints_binary_validity[batch_i, joint_i]
                loss += validity[0] * (-torch.log(volumes_batch_pred[batch_i, joint_i, index[0], index[1], index[2]] + 1e-6))
                n_losses += 1


        return loss / n_losses


class GeodesicLoss(nn.Module):
    def __init__(self):
        super().__init__()

    def _criterion(self, m1, m2):
        dev = m1.device
        batch_size = m1.shape[0]
        m = torch.bmm(m1, m2.transpose(1, 2))  # ~ (batch_size, 3, 3)

        cos = (m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2] - 1) / 2

        # bound [-1, 1]
        cos = torch.min(
            cos,
            torch.ones(batch_size).to(dev)
        )
        cos = torch.max(
            cos,
            torch.ones(batch_size).to(dev) * -1
        )

        return torch.acos(cos)

    def forward(self, m1, m2):
        theta = self._criterion(m1, m2)
        return theta.mean()


class HuberLoss(nn.Module):
    def __init__(self, threshold):
        super().__init__()

        self.c = np.float64(threshold)

    def _criterion(self, diff):
        diff[torch.abs(diff) <= self.c] = torch.abs(diff[torch.abs(diff) <= self.c])  # L1 norm within threshold
        
        diff[torch.abs(diff) > self.c] =\
            (torch.square(diff[torch.abs(diff) > self.c]) + np.square(self.c)) / (2 * self.c)

        return diff.mean()

    def forward(self, pred, gt):
        dev = pred.device

        diff = pred.to(dev) - gt.to(dev)
        return self._criterion(diff)


class SeparationLoss(nn.Module):
    """ see eq 8 in https://papers.nips.cc/paper/2018/file/24146db4eb48c718b84cae0a0799dcfc-Paper.pdf """

    def __init__(self, threshold):
        super().__init__()

        self.threshold = torch.tensor(np.square(threshold))
        # todo max thresh ?

    def forward(self, batched_kps):
        batch_size = batched_kps.shape[0]
        n_joints = batched_kps.shape[1]
        dev = batched_kps.device
        return torch.mean(torch.cat([
            torch.sum(torch.cat([
                torch.cat([
                    torch.max(
                        torch.tensor(0.0).to(dev),
                        self.threshold.to(dev) -\
                        torch.square(
                            torch.norm(batched_kps[batch_i, joint_i] - batched_kps[batch_i, other_joint])
                        ).to(dev)
                    ).unsqueeze(0)
                    for other_joint in range(n_joints)
                    if other_joint != joint_i
                ]).unsqueeze(0)
                for joint_i in range(n_joints)
            ])).unsqueeze(0)
            for batch_i in range(batch_size)
        ]))


class ProjectionLoss(nn.Module):
    """ project GT VS pred points to all views """

    def __init__(self):
        super().__init__()

    def forward(self, kps_world_gt, kps_world_pred, cameras, K, cam_preds, criterion=KeypointsMSESmoothLoss(threshold=20*20)):
        n_views = len(cameras)
        batch_size = kps_world_gt.shape[0]
        dev = cam_preds.device
        projections = project_to_weak_views(K, cam_preds, kps_world_pred)
        
        return torch.mean(torch.cat([
            criterion(
                projections[batch_i],
                torch.cat([
                    cameras[view_i][batch_i].world2proj()(
                        kps_world_gt[batch_i]
                    ).unsqueeze(0)
                    for view_i in range(n_views)
                ]).to(dev),  # gt
            ).unsqueeze(0)
            for batch_i in range(batch_size)
        ]))


class ScaleIndependentProjectionLoss(nn.Module):
    """ see eq 2 in https://arxiv.org/abs/2011.14679 """

    def __init__(self, criterion=nn.L1Loss()):
        super().__init__()

        self.criterion = criterion

    def forward(self, K, cam_preds, kps_world_pred, initial_keypoints):
        batch_size = cam_preds.shape[0]
        n_views = cam_preds.shape[1]
        dev = cam_preds.device

        projections = project_to_weak_views(
            K, cam_preds, kps_world_pred
        )
        return torch.mean(
            torch.cat([
                torch.cat([
                    self.criterion(
                        projections[batch_i, view_i].to(dev) /
                            torch.norm(projections[batch_i, view_i], p='fro'),
                        initial_keypoints[batch_i, view_i].to(dev) /
                            torch.norm(initial_keypoints[batch_i, view_i], p='fro')
                    ).unsqueeze(0)
                    for view_i in range(n_views)
                ]).unsqueeze(0)
                for batch_i in range(batch_size)
            ])
        )


class QuadraticProjectionLoss(nn.Module):
    """ inspired by `ScaleIndependentProjectionLoss` """

    def __init__(self, scale_kps, criterion=KeypointsMSESmoothLoss(threshold=1e2)):
        super().__init__()

        self.scale_kps = scale_kps
        self.criterion = criterion

    def forward(self, K, cam_preds, kps_world_pred, initial_keypoints):
        batch_size = cam_preds.shape[0]
        n_views = cam_preds.shape[1]
        dev = cam_preds.device

        projections = project_to_weak_views(
            K, cam_preds, kps_world_pred
        )
        penalizations = torch.cat([
            torch.cat([
                torch.sqrt(torch.square(
                    torch.norm(projections[batch_i, view_i], p='fro') /
                    torch.norm(initial_keypoints[batch_i, view_i], p='fro') - 1
                )).unsqueeze(0)
                for view_i in range(n_views)
            ]).unsqueeze(0).to(dev)  # pred
            for batch_i in range(batch_size)
        ])  # penalize ratio of area => I want it not too little, nor not too big

        return torch.mean(
            torch.cat([
                torch.cat([
                    self.criterion(
                        initial_keypoints[batch_i, view_i].to(dev) * self.scale_kps,
                        projections[batch_i, view_i].to(dev) * self.scale_kps
                    ).unsqueeze(0) * penalizations[batch_i, view_i]
                    for view_i in range(n_views)
                ])
                for batch_i in range(batch_size)
            ])
        )


class BodyStructureLoss(nn.Module):
    """ assuming human body <= 2.5 m """

    def __init__(self, threshold=2.5e3):
        super().__init__()

        half_body = threshold / 2.0
        self.threshold = half_body

    def forward(self, kps_world_pred):
        batch_size = kps_world_pred.shape[0]
        n_joints = kps_world_pred.shape[1]

        loss = torch.tensor(0.0).to(kps_world_pred.device)
        for batch_i in range(batch_size):  # todo batched
            for joint_i in range(n_joints):
                distance_from_pelvis = torch.norm(
                    kps_world_pred[batch_i, joint_i], p='fro'
                )
                if distance_from_pelvis > self.threshold:
                    loss += distance_from_pelvis

        normalization = batch_size * n_joints
        return loss / normalization


class WorldStructureLoss(nn.Module):
    """ assuming cameras are above the surface (i.e surface is NOT transparent) """

    def __init__(self, scale):
        super().__init__()

        self.scale = scale

    def _penalize_cam_z_location(self, cam_preds):
        cams_location = get_cam_location_in_world(
            cam_preds.view(-1, 4, 4)
        ).view(-1, 3)
        zs = cams_location[:, 2]  # Z coordinate in all views (of all batches)
        zs = zs / self.scale
        loss = torch.pow(2, -zs)  # exp blows up, zs > 0 => -> 0, else -> infty
        return torch.mean(loss)

    def _penalize_cam_rotation(self, cam_preds):
        """ assumption: no pitch in cam R => -sin(beta) ~ 0, see https://en.wikipedia.org/wiki/Rotation_matrix """

        sin_pitches = cam_preds.view(-1, 4, 4)[:, 0, 2]
        print(sin_pitches)
        loss = torch.exp(torch.square(sin_pitches))
        return torch.mean(loss)

    def forward(self, cam_preds):
        return self._penalize_cam_z_location(cam_preds) +\
            self._penalize_cam_rotation(cam_preds)