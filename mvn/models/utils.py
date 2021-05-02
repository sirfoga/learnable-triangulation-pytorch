import torch.optim as optim


def get_params(layer, as_list=True):
    params = layer.parameters()

    if as_list:
        return list(params)

    return params


def get_grad_params(model):
    return filter(lambda p: p.requires_grad, model.parameters())


def count_grad_params(layer):
    params = get_params(layer, as_list=False)
    return sum(
        p.data.nelement()
        for p in params
        if p.requires_grad
    )


def freeze_layer(layer):
    print('freezing {}'.format(layer._get_name()))

    for p in layer.parameters():
        p.requires_grad = False


def reset_layer(layer):
    try:
        layer.reset_parameters()
    except:
        for layer in layer.children():
            reset_layer(layer)


def show_params(model):
    tot = count_grad_params(model)

    for name, m in model.named_children():
        n_params = count_grad_params(m)
        as_perc = n_params / tot * 100.0
        print('{:>30} has {:10.0f} params (~ {:4.1f}) %'.format(
            name, n_params, as_perc
        ))

    print('total params: {:.0f}'.format(
        tot
    ))


def build_opt(model, config, base_optim=optim.Adam):
    show_params(model.backbone)

    freeze_layer(model.backbone.conv1)
    freeze_layer(model.backbone.bn1)
    freeze_layer(model.backbone.relu)
    freeze_layer(model.backbone.maxpool)
    freeze_layer(model.backbone.layer1)
    freeze_layer(model.backbone.layer2)
    freeze_layer(model.backbone.layer3)
    freeze_layer(model.backbone.layer4)

    # reset_layer(model.backbone.alg_confidences)
    # reset_layer(model.backbone.deconv_layers)
    # reset_layer(model.backbone.final_layer)

    show_params(model.backbone)

    if config.model.name == "vol":
        return base_optim(
            [
                {
                    'params': model.backbone.parameters()
                },
                {
                    'params': model.process_features.parameters(),
                    'lr': config.opt.process_features_lr if hasattr(config.opt, 'process_features_lr') else config.opt.lr
                },
                {
                    'params': model.volume_net.parameters(),
                    'lr': config.opt.volume_net_lr if hasattr(config.opt, 'volume_net_lr') else config.opt.lr
                }
            ],
            lr=config.opt.lr
        )

    return base_optim(
        filter(lambda p: p.requires_grad, model.backbone.parameters()),
        lr=config.opt.lr
    )
