# 1st edit by https://github.com/comfyanonymous/ComfyUI
# 2nd edit by Forge Official


import torch
import math
import os
import ldm_patched.modules.utils
import ldm_patched.modules.model_management
import ldm_patched.modules.model_detection
import ldm_patched.modules.model_patcher
import ldm_patched.modules.ops

import ldm_patched.controlnet.cldm
import ldm_patched.t2ia.adapter

from ldm_patched.modules.ops import main_stream_worker


def broadcast_image_to(tensor, target_batch_size, batched_number):
    current_batch_size = tensor.shape[0]
    #print(current_batch_size, target_batch_size)
    if current_batch_size == 1:
        return tensor

    per_batch = target_batch_size // batched_number
    tensor = tensor[:per_batch]

    if per_batch > tensor.shape[0]:
        tensor = torch.cat([tensor] * (per_batch // tensor.shape[0]) + [tensor[:(per_batch % tensor.shape[0])]], dim=0)

    current_batch_size = tensor.shape[0]
    if current_batch_size == target_batch_size:
        return tensor
    else:
        return torch.cat([tensor] * batched_number, dim=0)


def get_at(array, index, default=None):
    return array[index] if 0 <= index < len(array) else default


def compute_controlnet_weighting(control, cnet):

    positive_advanced_weighting = getattr(cnet, 'positive_advanced_weighting', None)
    negative_advanced_weighting = getattr(cnet, 'negative_advanced_weighting', None)
    advanced_frame_weighting = getattr(cnet, 'advanced_frame_weighting', None)
    advanced_sigma_weighting = getattr(cnet, 'advanced_sigma_weighting', None)
    advanced_mask_weighting = getattr(cnet, 'advanced_mask_weighting', None)

    transformer_options = cnet.transformer_options

    if positive_advanced_weighting is None and negative_advanced_weighting is None \
            and advanced_frame_weighting is None and advanced_sigma_weighting is None \
            and advanced_mask_weighting is None:
        return control

    cond_or_uncond = transformer_options['cond_or_uncond']
    sigmas = transformer_options['sigmas']
    cond_mark = transformer_options['cond_mark']

    if advanced_frame_weighting is not None:
        advanced_frame_weighting = torch.Tensor(advanced_frame_weighting * len(cond_or_uncond)).to(sigmas)
        assert advanced_frame_weighting.shape[0] == cond_mark.shape[0], \
            'Frame weighting list length is different from batch size!'

    if advanced_sigma_weighting is not None:
        advanced_sigma_weighting = torch.cat([advanced_sigma_weighting(sigmas)] * len(cond_or_uncond))

    for k, v in control.items():
        for i in range(len(v)):
            control_signal = control[k][i]

            if not isinstance(control_signal, torch.Tensor):
                continue

            B, C, H, W = control_signal.shape

            positive_weight = 1.0
            negative_weight = 1.0
            sigma_weight = 1.0
            frame_weight = 1.0

            if positive_advanced_weighting is not None:
                positive_weight = get_at(positive_advanced_weighting.get(k, []), i, 1.0)

            if negative_advanced_weighting is not None:
                negative_weight = get_at(negative_advanced_weighting.get(k, []), i, 1.0)

            if advanced_sigma_weighting is not None:
                sigma_weight = advanced_sigma_weighting

            if advanced_frame_weighting is not None:
                frame_weight = advanced_frame_weighting

            final_weight = positive_weight * (1.0 - cond_mark) + negative_weight * cond_mark
            final_weight = final_weight * sigma_weight * frame_weight

            if isinstance(advanced_mask_weighting, torch.Tensor):
                if advanced_mask_weighting.shape[0] != 1:
                    k_ = int(control_signal.shape[0] // advanced_mask_weighting.shape[0])
                    if control_signal.shape[0] == k_ * advanced_mask_weighting.shape[0]:
                        advanced_mask_weighting = advanced_mask_weighting.repeat(k_, 1, 1, 1)
                control_signal = control_signal * torch.nn.functional.interpolate(advanced_mask_weighting.to(control_signal), size=(H, W), mode='bilinear')

            control[k][i] = control_signal * final_weight[:, None, None, None]

    return control


class ControlBase:
    def __init__(self, device=None):
        self.cond_hint_original = None
        self.cond_hint = None
        self.strength = 1.0
        self.timestep_percent_range = (0.0, 1.0)
        self.global_average_pooling = False
        self.timestep_range = None
        self.transformer_options = {}

        if device is None:
            device = ldm_patched.modules.model_management.get_torch_device()
        self.device = device
        self.previous_controlnet = None

    def set_cond_hint(self, cond_hint, strength=1.0, timestep_percent_range=(0.0, 1.0)):
        self.cond_hint_original = cond_hint
        self.strength = strength
        self.timestep_percent_range = timestep_percent_range
        return self

    def pre_run(self, model, percent_to_timestep_function):
        self.timestep_range = (percent_to_timestep_function(self.timestep_percent_range[0]), percent_to_timestep_function(self.timestep_percent_range[1]))
        if self.previous_controlnet is not None:
            self.previous_controlnet.pre_run(model, percent_to_timestep_function)

    def set_previous_controlnet(self, controlnet):
        self.previous_controlnet = controlnet
        return self

    def cleanup(self):
        if self.previous_controlnet is not None:
            self.previous_controlnet.cleanup()
        if self.cond_hint is not None:
            del self.cond_hint
            self.cond_hint = None
        self.timestep_range = None

    def get_models(self):
        out = []
        if self.previous_controlnet is not None:
            out += self.previous_controlnet.get_models()
        return out

    def copy_to(self, c):
        c.cond_hint_original = self.cond_hint_original
        c.strength = self.strength
        c.timestep_percent_range = self.timestep_percent_range
        c.global_average_pooling = self.global_average_pooling

    def inference_memory_requirements(self, dtype):
        if self.previous_controlnet is not None:
            return self.previous_controlnet.inference_memory_requirements(dtype)
        return 0

    def control_merge(self, control_input, control_output, control_prev, output_dtype):
        out = {'input':[], 'middle':[], 'output': []}

        if control_input is not None:
            for i in range(len(control_input)):
                key = 'input'
                x = control_input[i]
                if x is not None:
                    x *= self.strength
                    if x.dtype != output_dtype:
                        x = x.to(output_dtype)
                out[key].insert(0, x)

        if control_output is not None:
            for i in range(len(control_output)):
                if i == (len(control_output) - 1):
                    key = 'middle'
                    index = 0
                else:
                    key = 'output'
                    index = i
                x = control_output[i]
                if x is not None:
                    if self.global_average_pooling:
                        x = torch.mean(x, dim=(2, 3), keepdim=True).repeat(1, 1, x.shape[2], x.shape[3])

                    x *= self.strength
                    if x.dtype != output_dtype:
                        x = x.to(output_dtype)

                out[key].append(x)

        out = compute_controlnet_weighting(out, self)

        if control_prev is not None:
            for x in ['input', 'middle', 'output']:
                o = out[x]
                for i in range(len(control_prev[x])):
                    prev_val = control_prev[x][i]
                    if i >= len(o):
                        o.append(prev_val)
                    elif prev_val is not None:
                        if o[i] is None:
                            o[i] = prev_val
                        else:
                            if o[i].shape[0] < prev_val.shape[0]:
                                o[i] = prev_val + o[i]
                            else:
                                o[i] += prev_val
        return out

class ControlNet(ControlBase):
    def __init__(self, control_model, global_average_pooling=False, device=None, load_device=None, manual_cast_dtype=None):
        super().__init__(device)
        self.control_model = control_model
        self.load_device = load_device
        self.control_model_wrapped = ldm_patched.modules.model_patcher.ModelPatcher(self.control_model, load_device=load_device, offload_device=ldm_patched.modules.model_management.unet_offload_device())
        self.global_average_pooling = global_average_pooling
        self.model_sampling_current = None
        self.manual_cast_dtype = manual_cast_dtype

    def get_control(self, x_noisy, t, cond, batched_number):
        to = self.transformer_options

        for conditioning_modifier in to.get('controlnet_conditioning_modifiers', []):
            x_noisy, t, cond, batched_number = conditioning_modifier(self, x_noisy, t, cond, batched_number)

        control_prev = None
        if self.previous_controlnet is not None:
            control_prev = self.previous_controlnet.get_control(x_noisy, t, cond, batched_number)

        if self.timestep_range is not None:
            if t[0] > self.timestep_range[0] or t[0] < self.timestep_range[1]:
                if control_prev is not None:
                    return control_prev
                else:
                    return None

        dtype = self.control_model.dtype
        if self.manual_cast_dtype is not None:
            dtype = self.manual_cast_dtype

        output_dtype = x_noisy.dtype
        if self.cond_hint is None or x_noisy.shape[2] * 8 != self.cond_hint.shape[2] or x_noisy.shape[3] * 8 != self.cond_hint.shape[3]:
            if self.cond_hint is not None:
                del self.cond_hint
            self.cond_hint = None
            self.cond_hint = ldm_patched.modules.utils.common_upscale(self.cond_hint_original, x_noisy.shape[3] * 8, x_noisy.shape[2] * 8, 'nearest-exact', "center").to(dtype)
        if x_noisy.shape[0] != self.cond_hint.shape[0]:
            self.cond_hint = broadcast_image_to(self.cond_hint, x_noisy.shape[0], batched_number)

        context = cond['c_crossattn']
        y = cond.get('y', None)
        if y is not None:
            y = y.to(dtype)
        timestep = self.model_sampling_current.timestep(t)
        x_noisy = self.model_sampling_current.calculate_input(t, x_noisy)

        controlnet_model_function_wrapper = to.get('controlnet_model_function_wrapper', None)

        if controlnet_model_function_wrapper is not None:
            wrapper_args = dict(x=x_noisy.to(dtype), hint=self.cond_hint, timesteps=timestep.float(),
                                context=context.to(dtype), y=y)
            wrapper_args['model'] = self
            wrapper_args['inner_model'] = self.control_model
            control = controlnet_model_function_wrapper(**wrapper_args)
        else:
            control = self.control_model(x=x_noisy.to(dtype), hint=self.cond_hint.to(self.device), timesteps=timestep.float(), context=context.to(dtype), y=y)
        return self.control_merge(None, control, control_prev, output_dtype)

    def copy(self):
        c = ControlNet(self.control_model, global_average_pooling=self.global_average_pooling, load_device=self.load_device, manual_cast_dtype=self.manual_cast_dtype)
        self.copy_to(c)
        return c

    def get_models(self):
        out = super().get_models()
        out.append(self.control_model_wrapped)
        return out

    def pre_run(self, model, percent_to_timestep_function):
        super().pre_run(model, percent_to_timestep_function)
        self.model_sampling_current = model.model_sampling

    def cleanup(self):
        self.model_sampling_current = None
        super().cleanup()

class ControlLoraOps:
    class Linear(torch.nn.Module):
        def __init__(self, in_features: int, out_features: int, bias: bool = True,
                    device=None, dtype=None) -> None:
            super().__init__()
            self.in_features = in_features
            self.out_features = out_features
            self.weight = None
            self.up = None
            self.down = None
            self.bias = None

        def forward(self, input):
            weight, bias, signal = ldm_patched.modules.ops.cast_bias_weight(self, input)
            with main_stream_worker(weight, bias, signal):
                if self.up is not None:
                    return torch.nn.functional.linear(input, weight + (torch.mm(self.up.flatten(start_dim=1), self.down.flatten(start_dim=1))).reshape(self.weight.shape).type(input.dtype), bias)
                else:
                    return torch.nn.functional.linear(input, weight, bias)

    class Conv2d(torch.nn.Module):
        def __init__(
            self,
            in_channels,
            out_channels,
            kernel_size,
            stride=1,
            padding=0,
            dilation=1,
            groups=1,
            bias=True,
            padding_mode='zeros',
            device=None,
            dtype=None
        ):
            super().__init__()
            self.in_channels = in_channels
            self.out_channels = out_channels
            self.kernel_size = kernel_size
            self.stride = stride
            self.padding = padding
            self.dilation = dilation
            self.transposed = False
            self.output_padding = 0
            self.groups = groups
            self.padding_mode = padding_mode

            self.weight = None
            self.bias = None
            self.up = None
            self.down = None


        def forward(self, input):
            weight, bias, signal = ldm_patched.modules.ops.cast_bias_weight(self, input)
            with main_stream_worker(weight, bias, signal):
                if self.up is not None:
                    return torch.nn.functional.conv2d(input, weight + (torch.mm(self.up.flatten(start_dim=1), self.down.flatten(start_dim=1))).reshape(self.weight.shape).type(input.dtype), bias, self.stride, self.padding, self.dilation, self.groups)
                else:
                    return torch.nn.functional.conv2d(input, weight, bias, self.stride, self.padding, self.dilation, self.groups)


class ControlLora(ControlNet):
    def __init__(self, control_weights, global_average_pooling=False, device=None):
        ControlBase.__init__(self, device)
        self.control_weights = control_weights
        self.global_average_pooling = global_average_pooling

    def pre_run(self, model, percent_to_timestep_function):
        super().pre_run(model, percent_to_timestep_function)
        controlnet_config = model.model_config.unet_config.copy()
        controlnet_config.pop("out_channels")
        controlnet_config["hint_channels"] = self.control_weights["input_hint_block.0.weight"].shape[1]
        self.manual_cast_dtype = model.manual_cast_dtype
        dtype = model.get_dtype()
        if self.manual_cast_dtype is None:
            class control_lora_ops(ControlLoraOps, ldm_patched.modules.ops.disable_weight_init):
                pass
        else:
            class control_lora_ops(ControlLoraOps, ldm_patched.modules.ops.manual_cast):
                pass
            dtype = self.manual_cast_dtype

        controlnet_config["operations"] = control_lora_ops
        controlnet_config["dtype"] = dtype
        self.control_model = ldm_patched.controlnet.cldm.ControlNet(**controlnet_config)
        self.control_model.to(ldm_patched.modules.model_management.get_torch_device())
        diffusion_model = model.diffusion_model
        sd = diffusion_model.state_dict()

        for k in sd:
            weight = sd[k]
            try:
                ldm_patched.modules.utils.set_attr(self.control_model, k, weight)
            except:
                pass

        for k in self.control_weights:
            if k not in {"lora_controlnet"}:
                ldm_patched.modules.utils.set_attr(self.control_model, k, self.control_weights[k].to(dtype).to(ldm_patched.modules.model_management.get_torch_device()))

    def copy(self):
        c = ControlLora(self.control_weights, global_average_pooling=self.global_average_pooling)
        self.copy_to(c)
        return c

    def cleanup(self):
        del self.control_model
        self.control_model = None
        super().cleanup()

    def get_models(self):
        out = ControlBase.get_models(self)
        return out

    def inference_memory_requirements(self, dtype):
        return ldm_patched.modules.utils.calculate_parameters(self.control_weights) * ldm_patched.modules.model_management.dtype_size(dtype) + ControlBase.inference_memory_requirements(self, dtype)

def load_controlnet(ckpt_path, model=None):
    controlnet_data = ldm_patched.modules.utils.load_torch_file(ckpt_path, safe_load=True)
    if "lora_controlnet" in controlnet_data:
        return ControlLora(controlnet_data)

    controlnet_config = None
    if "controlnet_cond_embedding.conv_in.weight" in controlnet_data: #diffusers format
        unet_dtype = ldm_patched.modules.model_management.unet_dtype()
        controlnet_config = ldm_patched.modules.model_detection.unet_config_from_diffusers_unet(controlnet_data, unet_dtype)
        diffusers_keys = ldm_patched.modules.utils.unet_to_diffusers(controlnet_config)
        diffusers_keys["controlnet_mid_block.weight"] = "middle_block_out.0.weight"
        diffusers_keys["controlnet_mid_block.bias"] = "middle_block_out.0.bias"

        count = 0
        loop = True
        while loop:
            suffix = [".weight", ".bias"]
            for s in suffix:
                k_in = "controlnet_down_blocks.{}{}".format(count, s)
                k_out = "zero_convs.{}.0{}".format(count, s)
                if k_in not in controlnet_data:
                    loop = False
                    break
                diffusers_keys[k_in] = k_out
            count += 1

        count = 0
        loop = True
        while loop:
            suffix = [".weight", ".bias"]
            for s in suffix:
                if count == 0:
                    k_in = "controlnet_cond_embedding.conv_in{}".format(s)
                else:
                    k_in = "controlnet_cond_embedding.blocks.{}{}".format(count - 1, s)
                k_out = "input_hint_block.{}{}".format(count * 2, s)
                if k_in not in controlnet_data:
                    k_in = "controlnet_cond_embedding.conv_out{}".format(s)
                    loop = False
                diffusers_keys[k_in] = k_out
            count += 1

        new_sd = {}
        for k in diffusers_keys:
            if k in controlnet_data:
                new_sd[diffusers_keys[k]] = controlnet_data.pop(k)

        leftover_keys = controlnet_data.keys()
        if len(leftover_keys) > 0:
            print("leftover keys:", leftover_keys)
        controlnet_data = new_sd

    pth_key = 'control_model.zero_convs.0.0.weight'
    pth = False
    key = 'zero_convs.0.0.weight'
    if pth_key in controlnet_data:
        pth = True
        key = pth_key
        prefix = "control_model."
    elif key in controlnet_data:
        prefix = ""
    else:
        net = load_t2i_adapter(controlnet_data)
        if net is None:
            print("error checkpoint does not contain controlnet or t2i adapter data", ckpt_path)
        return net

    if controlnet_config is None:
        unet_dtype = ldm_patched.modules.model_management.unet_dtype()
        controlnet_config = ldm_patched.modules.model_detection.model_config_from_unet(controlnet_data, prefix, unet_dtype, True).unet_config
    load_device = ldm_patched.modules.model_management.get_torch_device()
    manual_cast_dtype = ldm_patched.modules.model_management.unet_manual_cast(unet_dtype, load_device)
    if manual_cast_dtype is not None:
        controlnet_config["operations"] = ldm_patched.modules.ops.manual_cast
    controlnet_config.pop("out_channels")
    controlnet_config["hint_channels"] = controlnet_data["{}input_hint_block.0.weight".format(prefix)].shape[1]
    control_model = ldm_patched.controlnet.cldm.ControlNet(model_file_name=ckpt_path, **controlnet_config)

    if pth:
        if 'difference' in controlnet_data:
            if model is not None:
                ldm_patched.modules.model_management.load_models_gpu([model])
                model_sd = model.model_state_dict()
                for x in controlnet_data:
                    c_m = "control_model."
                    if x.startswith(c_m):
                        sd_key = "diffusion_model.{}".format(x[len(c_m):])
                        if sd_key in model_sd:
                            cd = controlnet_data[x]
                            cd += model_sd[sd_key].type(cd.dtype).to(cd.device)
            else:
                print("WARNING: Loaded a diff controlnet without a model. It will very likely not work.")

        class WeightsLoader(torch.nn.Module):
            pass
        w = WeightsLoader()
        w.control_model = control_model
        missing, unexpected = w.load_state_dict(controlnet_data, strict=False)
    else:
        missing, unexpected = control_model.load_state_dict(controlnet_data, strict=False)
    print(missing, unexpected)

    global_average_pooling = False
    filename = os.path.splitext(ckpt_path)[0]
    if filename.endswith("_shuffle") or filename.endswith("_shuffle_fp16"): #TODO: smarter way of enabling global_average_pooling
        global_average_pooling = True

    control = ControlNet(control_model, global_average_pooling=global_average_pooling, load_device=load_device, manual_cast_dtype=manual_cast_dtype)
    return control

class T2IAdapter(ControlBase):
    def __init__(self, t2i_model, channels_in, device=None):
        super().__init__(device)
        self.t2i_model = t2i_model
        self.channels_in = channels_in
        self.control_input = None

    def scale_image_to(self, width, height):
        unshuffle_amount = self.t2i_model.unshuffle_amount
        width = math.ceil(width / unshuffle_amount) * unshuffle_amount
        height = math.ceil(height / unshuffle_amount) * unshuffle_amount
        return width, height

    def get_control(self, x_noisy, t, cond, batched_number):
        to = self.transformer_options

        for conditioning_modifier in to.get('controlnet_conditioning_modifiers', []):
            x_noisy, t, cond, batched_number = conditioning_modifier(self, x_noisy, t, cond, batched_number)

        control_prev = None
        if self.previous_controlnet is not None:
            control_prev = self.previous_controlnet.get_control(x_noisy, t, cond, batched_number)

        if self.timestep_range is not None:
            if t[0] > self.timestep_range[0] or t[0] < self.timestep_range[1]:
                if control_prev is not None:
                    return control_prev
                else:
                    return None

        if self.cond_hint is None or x_noisy.shape[2] * 8 != self.cond_hint.shape[2] or x_noisy.shape[3] * 8 != self.cond_hint.shape[3]:
            if self.cond_hint is not None:
                del self.cond_hint
            self.control_input = None
            self.cond_hint = None
            width, height = self.scale_image_to(x_noisy.shape[3] * 8, x_noisy.shape[2] * 8)
            self.cond_hint = ldm_patched.modules.utils.common_upscale(self.cond_hint_original, width, height, 'nearest-exact', "center").float()
            if self.channels_in == 1 and self.cond_hint.shape[1] > 1:
                self.cond_hint = torch.mean(self.cond_hint, 1, keepdim=True)
        if x_noisy.shape[0] != self.cond_hint.shape[0]:
            self.cond_hint = broadcast_image_to(self.cond_hint, x_noisy.shape[0], batched_number)
        if self.control_input is None:
            self.t2i_model.to(x_noisy.dtype)
            self.t2i_model.to(self.device)

            controlnet_model_function_wrapper = to.get('controlnet_model_function_wrapper', None)

            if controlnet_model_function_wrapper is not None:
                wrapper_args = dict(hint=self.cond_hint.to(x_noisy.dtype))
                wrapper_args['model'] = self
                wrapper_args['inner_model'] = self.t2i_model
                wrapper_args['inner_t2i_model'] = self.t2i_model
                self.control_input = controlnet_model_function_wrapper(**wrapper_args)
            else:
                self.control_input = self.t2i_model(self.cond_hint.to(x_noisy))

            self.t2i_model.cpu()

        control_input = list(map(lambda a: None if a is None else a.clone(), self.control_input))
        mid = None
        if self.t2i_model.xl == True:
            mid = control_input[-1:]
            control_input = control_input[:-1]
        return self.control_merge(control_input, mid, control_prev, x_noisy.dtype)

    def copy(self):
        c = T2IAdapter(self.t2i_model, self.channels_in)
        self.copy_to(c)
        return c

def load_t2i_adapter(t2i_data):
    if 'adapter' in t2i_data:
        t2i_data = t2i_data['adapter']
    if 'adapter.body.0.resnets.0.block1.weight' in t2i_data: #diffusers format
        prefix_replace = {}
        for i in range(4):
            for j in range(2):
                prefix_replace["adapter.body.{}.resnets.{}.".format(i, j)] = "body.{}.".format(i * 2 + j)
            prefix_replace["adapter.body.{}.".format(i, )] = "body.{}.".format(i * 2)
        prefix_replace["adapter."] = ""
        t2i_data = ldm_patched.modules.utils.state_dict_prefix_replace(t2i_data, prefix_replace)
    keys = t2i_data.keys()

    if "body.0.in_conv.weight" in keys:
        cin = t2i_data['body.0.in_conv.weight'].shape[1]
        model_ad = ldm_patched.t2ia.adapter.Adapter_light(cin=cin, channels=[320, 640, 1280, 1280], nums_rb=4)
    elif 'conv_in.weight' in keys:
        cin = t2i_data['conv_in.weight'].shape[1]
        channel = t2i_data['conv_in.weight'].shape[0]
        ksize = t2i_data['body.0.block2.weight'].shape[2]
        use_conv = False
        down_opts = list(filter(lambda a: a.endswith("down_opt.op.weight"), keys))
        if len(down_opts) > 0:
            use_conv = True
        xl = False
        if cin == 256 or cin == 768:
            xl = True
        model_ad = ldm_patched.t2ia.adapter.Adapter(cin=cin, channels=[channel, channel*2, channel*4, channel*4][:4], nums_rb=2, ksize=ksize, sk=True, use_conv=use_conv, xl=xl)
    else:
        return None
    missing, unexpected = model_ad.load_state_dict(t2i_data)
    if len(missing) > 0:
        print("t2i missing", missing)

    if len(unexpected) > 0:
        print("t2i unexpected", unexpected)

    return T2IAdapter(model_ad, model_ad.input_channels)
