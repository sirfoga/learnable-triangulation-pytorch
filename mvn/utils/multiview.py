import numpy as np
import torch


def euclidean_to_homogeneous(points):
    """Converts euclidean points to homogeneous: [x y z] -> [x y z 1] foreach row

    Args:
        points numpy array or torch tensor of shape (N, M): N euclidean points of dimension M e.g N 3D world points ~ (N, 3)

    Returns:
        numpy array or torch tensor of shape (N, M + 1): homogeneous points
    """

    if isinstance(points, np.ndarray):
        return np.hstack([
            points,
            np.ones((len(points), 1))
        ])  # ~ (N, 4)
    elif torch.is_tensor(points):
        return torch.cat([
            points,
            torch.ones(
                (points.shape[0], 1), dtype=points.dtype, device=points.device
            )
        ], dim=1)


def homogeneous_to_euclidean(points):
    """Converts homogeneous points to euclidean: [x y z w] -> [x/w y/w z/w]

    Args:
        points numpy array or torch tensor of shape (N, M + 1): N homogeneous points of dimension M

    Returns:
        numpy array or torch tensor of shape (N, M): euclidean points
    """
    if isinstance(points, np.ndarray):
        return (points.T[:-1] / points.T[-1]).T  # w is last
    elif torch.is_tensor(points):
        return (points.transpose(1, 0)[:-1] / points.transpose(1, 0)[-1]).transpose(1, 0)


# todo refactor using fs like _myproj ...
class Camera:
    def __init__(self, R, t, K, dist=None, name=""):
        self.R = np.array(R).copy()  # 3 x 3

        self.t = np.array(t).copy()
        self.t = self.t.reshape(3, 1)

        self.K = np.array(K).copy()  # intrinsic ~ 3 x 3

        self.dist = dist
        if self.dist is not None:
            self.dist = np.array(self.dist).copy().flatten()

        self.name = name

    def update_after_crop(self, bbox):
        left, upper, _, _ = bbox  # unpack

        cx, cy = self.K[0, 2], self.K[1, 2]

        new_cx = cx - left
        new_cy = cy - upper

        self.K[0, 2], self.K[1, 2] = new_cx, new_cy

    def update_after_resize(self, image_shape, new_image_shape):
        height, width = image_shape  # original image resolution
        new_height, new_width = new_image_shape

        fx, fy, cx, cy = self.K[0, 0], self.K[1, 1], self.K[0, 2], self.K[1, 2]

        new_fx = fx * (new_width / width)
        new_fy = fy * (new_height / height)
        new_cx = cx * (new_width / width)
        new_cy = cy * (new_height / height)

        self.K[0, 0], self.K[1, 1], self.K[0, 2], self.K[1, 2] = new_fx, new_fy, new_cx, new_cy

    def update_extrinsics(self, Rt):
        E = Rt.copy().dot(self.extrinsics)
        self.R = E[:3, :3]
        self.t = E[:3, 3].reshape(3, 1)

    def scale_K(self, scaling):
        self.K[0, 0] /= scaling
        self.K[1, 1] /= scaling

    def scale_extrinsics(self, scaling):
        self.t /= scaling

    @property
    def extrinsics(self):  # 3D world -> 3D camera space
        return np.hstack([self.R, self.t])  # ~ 3 x 4 (rotation 3 x 3 + translation 3 x 1)

    @property
    def projection(self):  # 3D world -> 3D camera space -> 2D camera
        return self.K.dot(self.extrinsics)  # ~ 3 x 4

    @property
    def extrinsics_padded(self):
        return np.vstack([
            self.extrinsics,
            [0, 0, 0, 1]
        ])  # ~ 4 x 4, to allow inverse
    
    @property
    def intrinsics_padded(self):
        return np.hstack([
            self.K,
            np.expand_dims(np.zeros(3), axis=0).T
        ])  # 3 x 4

    def cam2world(self):  # todo faster by .T
        """ 3D camera space (3D, x y z 1) -> 3D world (euclidean) """

        def _f(x):
            homo = euclidean_to_homogeneous(x)  # [x y z] -> [x y z 1]
            inv = torch.inverse(torch.tensor(self.extrinsics_padded.T))  # N x 4
            eucl = homo.type @ inv
            return homogeneous_to_euclidean(eucl)  # N x 4 -> N x 3

        return _f

    def world2cam(self):
        """ 3D world (N x 3) -> 3D camera space (N x 3) homo """

        def _f(x):
            return euclidean_to_homogeneous(
                x  # [x y z] -> [x y z 1]
            ) @ self.extrinsics.T  # N x 3

        return _f

    def world2proj(self):
        """ 3D world (N x 3) -> 2D image (N x 2) """

        def _f(x):
            dev = x.device
            homo = euclidean_to_homogeneous(x).to(dev)
            proj = torch.tensor(self.projection.T).to(dev).type(homo.type())
            return homogeneous_to_euclidean(homo @ proj)

        return _f

    def cam2proj(self):
        """ 3D camera space (N x 3) homo -> 2D image (N x 2) """

        def _f(x):
            return homogeneous_to_euclidean(
                x @ self.K.T  # just apply intrinsic
            )  # [x y f] -> [x/f y/f]
        
        return _f

    def cam2other(self, other):
        """ 3D camera space (4D, x y z 1) -> 3D world (homo) -> 3D other camera space (4D, x y z 1) -> 2D other (proj) """

        def _f(x):
            in_other_cam = self.cam2cam(other)(x)
            homo = in_other_cam @ torch.tensor(other.intrinsics_padded.T)

            # ... or equivalently:
            # m = other.intrinsics_padded.dot(
            #     other.extrinsics_padded
            # ).dot(
            #     np.linalg.inv(self.extrinsics_padded)
            # )
            # homo = x @ m.T
            
            return homogeneous_to_euclidean(homo)

        return _f

    def cam2cam(self, other):
        """ 3D camera space (4D, x y z 1) -> 3D world (homo) -> 3D other camera space (4D, x y z 1) """
        
        def _f(x):
            inv = torch.inverse(torch.tensor(self.extrinsics_padded.T))  # todo faster by .T
            back2world = x @ inv  # N x 4
            return back2world @ torch.tensor(other.extrinsics_padded.T)  # N x 4

            # ... or equivalently:
            # m = other.extrinsics_padded.dot(
            #     np.linalg.inv(self.extrinsics_padded)
            # )
            # return x @ m.T

        return _f

    def __str__(self):
        return 'K:\n{}\nR | t:\n{}'.format(
            str(self.K),
            str(self.extrinsics)
        )


def build_intrinsics(translation=(0, 0), f=(1, 1), shear=1):
    return np.array([
        [f[0], shear / f[0], translation[0]],
        [0   , f[1]        , translation[1]],
        [0.  , 0.          , 1.            ]
    ])


def project_3d_points_to_image_plane_without_distortion(proj_matrix, points_3d, convert_back_to_euclidean=True):
    """Project 3D points to image plane not taking into account distortion
    Args:
        proj_matrix numpy array or torch tensor of shape (3, 4): projection matrix
        points_3d numpy array or torch tensor of shape (N, 3): 3D points
        convert_back_to_euclidean bool: if True, then resulting points will be converted to euclidean coordinates
    NOTE: division by zero can be here if z = 0
    Returns:
        numpy array or torch tensor of shape (N, 2): 3D points projected to image plane
    """
    if isinstance(proj_matrix, np.ndarray) and isinstance(points_3d, np.ndarray):
        result = euclidean_to_homogeneous(points_3d) @ proj_matrix.T  # batched dot
        if convert_back_to_euclidean:
            result = homogeneous_to_euclidean(result)
        return result
    elif torch.is_tensor(proj_matrix) and torch.is_tensor(points_3d):
        result = euclidean_to_homogeneous(points_3d) @ proj_matrix.t()
        if convert_back_to_euclidean:
            result = homogeneous_to_euclidean(result)
        return result


def triangulate_point_from_multiple_views_linear(proj_matricies, points, convert_to_euclidean=True):  # todo use confidences
    """Triangulates one point from multiple (N) views using direct linear transformation (DLT). For more information look at "Multiple view geometry in computer vision", Richard Hartley and Andrew Zisserman, 12.2 (p. 312).

    Args:
        proj_matricies numpy array of shape (N, 3, 4): sequence of projection matricies (3x4)
        points numpy array of shape (N, 2): sequence of points' coordinates

    Returns:
        point_3d numpy array of shape (3,): triangulated point
    """

    n_views = len(proj_matricies)
    A = np.zeros((2 * n_views, 4))

    for j in range(n_views):
        A[j * 2 + 0] = points[j][0] * proj_matricies[j][2, :] - proj_matricies[j][0, :]  # x
        A[j * 2 + 1] = points[j][1] * proj_matricies[j][2, :] - proj_matricies[j][1, :]  # y

    u, s, vh = np.linalg.svd(A, full_matrices=False)
    point_3d = vh[3,:]  # solution as the unit singular vector corresponding to the smallest singular value of A
    
    if convert_to_euclidean:
        point_3d = homogeneous_to_euclidean(point_3d)  # homo -> euclid

    return point_3d


def triangulate_point_from_multiple_views_linear_torch(proj_matricies, points, confidences=None, convert_to_euclidean=True):
    """ = triangulate_point_from_multiple_views_linear but for PyTorch.
    Args:
        proj_matricies torch tensor of shape (n_views, 3, 4): sequence of projection matricies (3x4)
        points torch tensor of of shape (n_views, 2): sequence of points' coordinates (in camera coordinates, i.e homogeneous)
        confidences None or torch tensor of shape (n_views, ): confidences of points [0.0, 1.0]. If None, all confidences are supposed to be 1.0
    Returns:
        point_3d numpy torch tensor of shape (3, ): triangulated point in 3D world view (coordinates)
    """

    n_views = len(proj_matricies)

    if confidences is None:
        confidences = torch.ones(
            n_views, device=points.device  # same device
        )

    A = proj_matricies[:, 2:3].expand(n_views, 2, 4) * points.view(n_views, 2, 1)
    A -= proj_matricies[:, :2]
    A *= confidences.view(-1, 1, 1)

    try:
        u, s, vh = torch.svd(A.view(-1, 4))  # ~ (8, n_views=4)
    except:  # usually SVD may not converge because of nan values
        print(proj_matricies)
        print(points)
        print(confidences)
        print(convert_to_euclidean)
        print(A)

    point_3d = -vh[:, 3]  # ~ (n_views=4,)

    if convert_to_euclidean:
        point_3d = homogeneous_to_euclidean(
            point_3d.unsqueeze(0)  # ~ (n_views=4, 1)
        )[0]  # [x y z w] ->   # [x/w y/w z/w], homo -> euclid

    return point_3d  # ~ (n_views=3,)


def triangulate_from_multiple_views_sii(proj_matricies, points, n_iter=2):
    """ from https://github.com/edoRemelli/DiffDLT/blob/master/dlt.py#L42. This module lifts batch_size 2d detections obtained from n_views viewpoints to 3D using the DLT method. It computes the eigenvector associated to the smallest eigenvalue using the Shifted Inverse Iterations algorithm.
    Args:
        proj_matricies torch tensor of shape (batch_size, n_views, 3, 4): sequence of projection matricies (3x4)
        points torch tensor of shape (batch_size, n_views, 2): sequence of points' coordinates
    Returns:
        point_3d torch tensor of shape (batch_size, 3): triangulated points
    """

    batch_size = proj_matricies.shape[0]
    n_views = proj_matricies.shape[1]

    # assemble linear system
    A = proj_matricies[:, :, 2:3].expand(batch_size, n_views, 2, 4) * points.view(-1, n_views, 2, 1)
    A -= proj_matricies[:, :, :2]
    A = A.view(batch_size, -1, 4)

    AtA = A.permute(0, 2, 1).matmul(A).float()
    I = torch.eye(4).reshape(1, 4, 4).repeat(batch_size, 1, 1).to(A.device)
    B = AtA + 0.001 * I  # avoid numerical errors

    # initialize normalized random vector
    bk = torch.rand(batch_size, 4, 1).float().to(AtA.device)
    norm_bk = torch.sqrt(bk.permute(0, 2, 1).matmul(bk))
    bk = bk / norm_bk
    
    for k in range(n_iter):
        bk, _ = torch.solve(bk, B)
        norm_bk = torch.sqrt(bk.permute(0, 2, 1).matmul(bk))
        bk = bk / norm_bk

    point_3d_homo = -bk.squeeze(-1)
    point_3d = homogeneous_to_euclidean(point_3d_homo)

    return point_3d


def triangulate_batch_of_points_using_gpu_friendly_svd(proj_matricies_batch, points_batch):
    batch_size, n_views, n_joints = points_batch.shape[:3]
    point_3d_batch = torch.zeros(
        batch_size,
        n_joints,
        3,  # because we're in 3D space
        device=points_batch.device
    )  # ~ (batch_size=8, n_joints=17, 3)

    # todo: faster loop (use Tensor)
    for batch_i in range(batch_size):  # 8
        for joint_i in range(n_joints):  # 17
            points = points_batch[batch_i,:, joint_i,:]

            point_3d = triangulate_from_multiple_views_sii(
                proj_matricies_batch[batch_i].unsqueeze(0),  # ~ (n_views=4, 3, 4)
                points.unsqueeze(0)  # ~ (n_views=4, 2)
            )  # ~ (3, )
            point_3d_batch[batch_i, joint_i] = point_3d

    return point_3d_batch


def triangulate_points_in_camspace(points_batch, matrices_batch, confidences_batch=None):
    n_joints = points_batch.shape[1]
    triangulator = triangulate_point_from_multiple_views_linear_torch

    point_3d_batch = torch.zeros(
        n_joints,
        3,
    )  # ~ (batch_size=8, n_joints=17, 3D)

    for joint_i in range(n_joints):  # triangulate joint
        points = points_batch[:, joint_i]  # ~ (n_views=4, 2)

        if not (confidences_batch is None):
            confidences = confidences_batch[:, joint_i]
            point_3d = triangulator(
                matrices_batch.cpu(),
                points.cpu(),
                confidences=confidences.cpu()
            )
        else:
            point_3d = triangulator(
                matrices_batch,
                points
            )

        point_3d_batch[joint_i] = point_3d

    return point_3d_batch


def triangulate_batch_of_points_in_cam_space(matrices_batch, points_batch, triangulator=triangulate_points_in_camspace, confidences_batch=None):
    batch_size = points_batch.shape[0]
    return torch.cat([
        triangulator(
            points_batch[batch_i],
            matrices_batch[batch_i],
            confidences_batch[batch_i] if not (confidences_batch is None) else None
        ).unsqueeze(0)
        for batch_i in range(batch_size)
    ])


def triangulate_batch_of_points(proj_matricies_batch, points_batch, triangulator, confidences_batch=None):
    """ proj matrices, keypoints 2D (pred), confidences """

    batch_size, _, n_joints = points_batch.shape[:3]
    point_3d_batch = torch.zeros(
        batch_size,
        n_joints,
        3,
        device=points_batch.device
    )  # ~ (batch_size=8, n_joints=17, 3)

    for batch_i in range(batch_size):  # 8
        for joint_i in range(n_joints):  # 17
            points = points_batch[batch_i, :, joint_i, :]

            if not (confidences_batch is None):
                confidences = confidences_batch[batch_i,:, joint_i]
                point_3d = triangulator(
                    proj_matricies_batch[batch_i],  # ~ (n_views=4, 3, 4)
                    points,  # ~ (n_views=4, 2)
                    confidences=confidences  # ~ (n_views=4, )
                )
            else:  # do not use confidences
                point_3d = triangulator(
                    proj_matricies_batch[batch_i],  # ~ (n_views=4, 3, 4)
                    points  # ~ (n_views=4, 2)
                )

            point_3d_batch[batch_i, joint_i] = point_3d  # ~ (3, )

    return point_3d_batch


def calc_reprojection_error_matrix(keypoints_3d, keypoints_2d_list, proj_matricies):
    reprojection_error_matrix = []
    for keypoints_2d, proj_matrix in zip(keypoints_2d_list, proj_matricies):
        keypoints_2d_projected = project_3d_points_to_image_plane_without_distortion(proj_matrix, keypoints_3d)
        reprojection_error = 1 / 2 * np.sqrt(np.sum((keypoints_2d - keypoints_2d_projected) ** 2, axis=1))
        reprojection_error_matrix.append(reprojection_error)

    return np.vstack(reprojection_error_matrix).T


def _2camspace(ext_from, ext_to):
    return torch.mm(
        ext_to,
        torch.inverse(ext_from)  # todo faster by .T
    ).T


def _2proj(ext_from, ext_to, K_to):
    trans_camspace = _2camspace(ext_from, ext_to)

    def _f(x):
        to_camspace = euclidean_to_homogeneous(x) @ trans_camspace
        projected = to_camspace @ K_to.T
        return homogeneous_to_euclidean(projected)

    return _f


def _my_proj(ext, K):
    projection = torch.mm(
        K.type(torch.get_default_dtype()),
        ext.type(torch.get_default_dtype())
    ).T

    def _f(x):
        homo = euclidean_to_homogeneous(x)
        return homogeneous_to_euclidean(
            homo.type(projection.type()) @ projection
        )

    return _f


def project2weak_views(K, cam_preds, kps_world_pred, where='world'):
    """ assuming https://en.wikipedia.org/wiki/3D_projection#Weak_perspective_projection """

    batch_size = cam_preds.shape[0]
    n_views = cam_preds.shape[1]
    dev = cam_preds.device
    return torch.cat([
        torch.cat([
            _my_proj(
                torch.eye(4).to(dev) if (where == 'master' and view_i == 0) else cam_preds[batch_i, view_i],
                K.to(dev)
            )(kps_world_pred[batch_i]).unsqueeze(0)
            for view_i in range(n_views)
        ]).unsqueeze(0).to(dev)  # pred
        for batch_i in range(batch_size)
    ])  # project DLT-ed points in all views


def prepare_weak_cams_for_dlt(cams, K, where="world"):
    batch_size = cams.shape[0]
    n_views = cams.shape[1]
    full_cams = torch.empty((batch_size, n_views, 3, 4))

    for batch_i in range(batch_size):
        if where == 'world':
            full_cams[batch_i] = torch.cat([
                torch.mm(
                    K,
                    cams[batch_i, view_i]
                ).unsqueeze(0)
                for view_i in range(n_views)
            ])
        elif where == 'master':
            full_cams[batch_i] = torch.cat([
                K.unsqueeze(0),  # doing DLT in master (0)'s camspace
                torch.mm(
                    K,
                    cams[batch_i, 1]
                ).unsqueeze(0),
                torch.mm(
                    K,
                    cams[batch_i, 2]
                ).unsqueeze(0),
                torch.mm(
                    K,
                    cams[batch_i, 3]
                ).unsqueeze(0),
            ])  # ~ 4, 3, 4

    return full_cams  # ~ batch_size, 4, 3, 4