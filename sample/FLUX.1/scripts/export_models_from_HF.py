#===----------------------------------------------------------------------===#
# This repository uses FLUX as the base model.
# Users must comply with FLUX's license when using this code. Please refer to 
# https://github.com/black-forest-labs/flux/tree/main/model_licenses
#===----------------------------------------------------------------------===#
#
# Copyright (C) 2024 Sophgo Technologies Inc.  All rights reserved.
#
# SOPHON-DEMO is licensed under the 2-Clause BSD License except for the
# third-party components.
#
#===----------------------------------------------------------------------===#
import argparse
import os
from diffusers import FluxPipeline 
from diffusers.models import attention_processor 
import torch
from transformers.modeling_attn_mask_utils import _create_4d_causal_attention_mask

clip_model_save_path = "../models/onnx_pt/clip"
t5_model_save_path = "../models/onnx_pt/t5"
dev_transformer_save_path = "../models/onnx_pt/dev_transformer"
schnell_transformer_save_path = "../models/onnx_pt/schnell_transformer"
vae_decoder_model_save_path = "../models/onnx_pt/vae"

if not os.path.exists(clip_model_save_path):
    os.makedirs(clip_model_save_path)
if not os.path.exists(t5_model_save_path):
    os.makedirs(t5_model_save_path)
if not os.path.exists(dev_transformer_save_path):
    os.makedirs(dev_transformer_save_path)
if not os.path.exists(schnell_transformer_save_path):
    os.makedirs(schnell_transformer_save_path)
if not os.path.exists(vae_decoder_model_save_path):
    os.makedirs(vae_decoder_model_save_path)

device = torch.device('cpu')
data_type = torch.float32

# input shapes for 1684x: 512 max-sequence-length; 1024 * 1024 img
original_input_shapes = {
    "clip": {
        "clip_head":[(1, 77)],
        "clip_block":[(1, 77, 768)],
        "clip_tail":[(1, 77, 768), (1, 77)]},

    "t5": {
        "t5_head":[(1, 512)],
        "t5_block":[(1, 512, 4096)],
        "t5_tail":[(1, 512, 4096)]},

    "dev": {
        "dev_head":[(1, 4096, 64), (1), (1), (1, 768), (1, 512, 4096)],
        "dev_mm_trans_block":[(1, 4096, 3072),(1, 512, 3072),(1, 3072),(1, 1, 4608, 1, 64, 2, 2)],
        "dev_single_trans_block":[(1, 4608, 3072),(1, 3072),(1, 4608, 1, 64, 2, 2)],
        "dev_tail":[(1, 4096, 3072),(1, 3072)]},

    "schnell": {
        "schnell_head":[(1, 4096, 64), (1), (1, 768), (1, 512, 4096)],
        "schnell_mm_trans_block":[(1, 4096, 3072),(1, 512, 3072),(1, 3072),(1, 4608, 1, 64, 2, 2)],
        "schnell_single_trans_block":[(1, 4608, 3072),(1, 3072),(1, 4608, 1, 64, 2, 2)],
        "schnell_tail":[(1, 4096, 3072),(1, 3072)]},

    "vae": {
        "vae_decoder":[(1, 16, 128, 128)]},
}

# input shapes for 1688: 256 max-sequence-length; 512 * 512 img
halved_input_shapes = {
    "clip": {
        "clip_head":[(1, 77)],
        "clip_block":[(1, 77, 768)],
        "clip_tail":[(1, 77, 768), (1, 77)]},

    "t5": {
        "t5_head":[(1, 256)],
        "t5_block":[(1, 256, 4096)],
        "t5_tail":[(1, 256, 4096)]},

    "dev": {
        "dev_head":[(1, 1024, 64), (1), (1), (1, 768), (1, 256, 4096)],
        "dev_mm_trans_block":[(1, 1024, 3072),(1, 256, 3072),(1, 3072),(1, 1, 1280, 64, 2, 2)],
        "dev_single_trans_block":[(1, 1280, 3072),(1, 3072),(1, 1280, 1, 64, 2, 2)],
        "dev_tail":[(1, 1024, 3072),(1, 3072)]},

    "schnell": {
        "schnell_head":[(1, 1024, 64), (1), (1, 768), (1, 256, 4096)],
        "schnell_mm_trans_block":[(1, 1024, 3072),(1, 256, 3072),(1, 3072),(1, 1280, 1, 64, 2, 2)],
        "schnell_single_trans_block":[(1, 1280, 3072),(1, 3072),(1, 1280, 1, 64, 2, 2)],
        "schnell_tail":[(1, 1024, 3072),(1, 3072)]},

    "vae": {
        "vae_decoder":[(1, 16, 64, 64)]},
}

def eval_mode(model):
    model.eval()
    for p in model.parameters():
        p.requires_grad = False

def build_input(input_shapes):
    fake_input = []
    for shape in input_shapes:
        fake_input.append(torch.randn(shape, dtype = data_type, device = device))
    return fake_input

def make_apply_rope(img_size):
    if img_size == 512:
        shapes = [1, 1280, 1, 64, 2, 2]
    elif img_size == 1024:
        shapes = [1, 4608, 1, 64, 2, 2]
    def _apply_rope_(xq, xk, freqs_cis):
        freqs_cis = freqs_cis.reshape(*shapes)
        xq_ = xq.float().reshape(*xq.shape[:-1], -1, 1, 2)
        xk_ = xk.float().reshape(*xk.shape[:-1], -1, 1, 2)
        xq_out = freqs_cis[..., 0] * xq_[..., 0] + freqs_cis[..., 1] * xq_[..., 1]
        xk_out = freqs_cis[..., 0] * xk_[..., 0] + freqs_cis[..., 1] * xk_[..., 1]
        return xq_out.reshape(*xq.shape).type_as(xq), xk_out.reshape(*xk.shape).type_as(xk)
    return _apply_rope_

def export_clip_head(model, input_shapes):
    eval_mode(model)
    dummy_input = torch.randint(0, 1000, input_shapes[0], dtype = torch.int64)
    def build_clip_head(input_ids):
        with torch.no_grad():
            prompt_embeds = model(input_ids)
            return prompt_embeds
    traced_model = torch.jit.trace(build_clip_head, dummy_input)
    traced_model.save(os.path.join(clip_model_save_path, 'clip_head.pt'))

def export_clip_block(model, idx, input_shapes):
    eval_mode(model)
    dummy_input = build_input(input_shapes)
    causal_attention = _create_4d_causal_attention_mask([1, input_shapes[0][1]], data_type, device = device)
    def build_clip_block(hidden_state):
        with torch.no_grad():
            hidden_states = model(hidden_state, None, causal_attention, False)
            return hidden_states[0]
    traced_model = torch.jit.trace(build_clip_block, dummy_input)
    traced_model.save(os.path.join(clip_model_save_path, f'clip_block_{idx}.pt'))

def export_clip_tail(model, input_shapes):
    eval_mode(model)
    dummy_input = build_input(input_shapes)
    dummy_input[1] = torch.randint(0, 1000, input_shapes[1], dtype = torch.int64)
    def build_clip_tail(last_hidden_state, input_ids):
        with torch.no_grad():
            last_hidden_state = model(last_hidden_state)
            idx = input_ids.argmax(dim=-1)
            pooled_output = last_hidden_state[0, idx, :]
            return pooled_output
    traced_model = torch.jit.trace(build_clip_tail, dummy_input)
    torch.onnx.export(traced_model, dummy_input, os.path.join(clip_model_save_path, 'clip_tail.onnx'))
    traced_model.save(os.path.join(clip_model_save_path, 'clip_tail.pt'))

def export_clip(model, input_shapes):
    export_clip_head(model.text_model.embeddings, input_shapes['clip_head'])
    for idx in range(12):
        export_clip_block(model.text_model.encoder.layers[idx], idx, input_shapes['clip_block'])
    export_clip_tail(model.text_model.final_layer_norm, input_shapes['clip_tail'])

def export_t5_head(model, input_shapes):
    eval_mode(model)
    dummy_input = torch.randint(0, 1000, input_shapes[0], dtype = torch.int64)
    def build_t5_head(input_ids):
        with torch.no_grad():
            prompt_embeds = model(input_ids)
            return prompt_embeds
    traced_model = torch.jit.trace(build_t5_head, dummy_input)
    torch.onnx.export(traced_model, dummy_input, os.path.join(t5_model_save_path, 't5_head.onnx'))

def export_t5_block(model, idx, temp_value, input_shapes):
    eval_mode(model)
    dummy_input = build_input(input_shapes)
    t = torch.zeros(1, 1, 1, input_shapes[0][1])

    def build_t5_first_block(hidden_states):
        with torch.no_grad():
            hidden_states = model[0](hidden_states = hidden_states, attention_mask = t)
            hidden_states = hidden_states[0]
            hidden_states = model[-1](hidden_states)
            return hidden_states

    def build_t5_block(hidden_states):
        with torch.no_grad():
            hidden_states = model[0](hidden_states = hidden_states, attention_mask = t, position_bias = temp_value)
            hidden_states = hidden_states[0]
            hidden_states = model[-1](hidden_states)
            return hidden_states

    traced_model = torch.jit.trace(build_t5_block, dummy_input) if idx != 0 else torch.jit.trace(build_t5_first_block, dummy_input)
    torch.onnx.export(traced_model, dummy_input, os.path.join(t5_model_save_path, f't5_block_{idx}.onnx'))

def export_t5_tail(model, input_shapes):
    eval_mode(model)
    dummy_input = build_input(input_shapes)
    def build_t5_tail(last_hidden_state):
        with torch.no_grad():
            prompt_embeds = model(last_hidden_state)
            return prompt_embeds
    traced_model = torch.jit.trace(build_t5_tail, dummy_input)
    traced_model.save(os.path.join(t5_model_save_path, 't5_tail.pt'))

def export_t5(model, input_shapes):
    t5_token_ids = [[389, 8293, 18174, 7494, 3, 9, 15184, 1]]
    t5_token_ids[0] = t5_token_ids[0] + [0] * (input_shapes['t5_head'][0][1] - 8)
    t5_token_ids = torch.tensor(t5_token_ids, dtype = torch.int64)
    temp_value = model.encoder.block[0].layer[0](model.encoder.embed_tokens(t5_token_ids))[2:][0].detach().requires_grad_(False)
    export_t5_head(model.encoder.embed_tokens, input_shapes=input_shapes['t5_head'])
    for idx in range(24):
        export_t5_block(model.encoder.block[idx].layer, idx, temp_value, input_shapes=input_shapes['t5_block'])
    export_t5_tail(model.encoder.final_layer_norm, input_shapes=input_shapes['t5_tail'])

def export_transformer(model, flux_type, input_shapes):
    eval_mode(model)
    # export head
    class DevHead(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.time_text_embed = model.time_text_embed
            self.context_embedder =model.context_embedder
            self.x_embedder =model.x_embedder
        def forward(
                self,
                hidden_states,
                timestep,
                guidance,
                pooled_projections,
                encoder_hidden_states,
                ):
            hidden_states = self.x_embedder(hidden_states)
            temb = (
                self.time_text_embed(timestep, pooled_projections)
                if guidance is None
                else self.time_text_embed(timestep, guidance, pooled_projections)
            )
            encoder_hidden_states = self.context_embedder(encoder_hidden_states)
            return temb, encoder_hidden_states, hidden_states
    class SchnellHead(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.time_text_embed = model.time_text_embed
            self.context_embedder =model.context_embedder
            self.x_embedder =model.x_embedder
        def forward(
                self,
                hidden_states,
                timestep,
                pooled_projections,
                encoder_hidden_states,
                ):
            hidden_states = self.x_embedder(hidden_states)
            temb = (self.time_text_embed(timestep, pooled_projections))
            encoder_hidden_states = self.context_embedder(encoder_hidden_states)
            return temb, encoder_hidden_states, hidden_states

    dummy_input = build_input(input_shapes[f'{flux_type}_head'])
    traced_model = torch.jit.trace(DevHead() if flux_type == "dev" else SchnellHead(), dummy_input)
    traced_model.save(os.path.join(dev_transformer_save_path if flux_type == "dev" else schnell_transformer_save_path, f"{flux_type}_head.pt"))

    class FluxMMDiT(torch.nn.Module):
        def __init__(self, idx):
            super().__init__()
            self.block = model.transformer_blocks[idx]

        def forward(
                self, 
                hidden_states, 
                encoder_hidden_states, 
                temb, 
                image_rotary_emb,
                ):
            encoder_hidden_states, hidden_states = self.block(
                        hidden_states,
                        encoder_hidden_states,
                        temb,
                        image_rotary_emb,)
            return encoder_hidden_states, hidden_states
    dummy_input = build_input(input_shapes[f'{flux_type}_mm_trans_block'])
    for idx in range(19):
        traced_model = torch.jit.trace(FluxMMDiT(idx), dummy_input)
        traced_model.save(os.path.join(dev_transformer_save_path if flux_type == "dev" else schnell_transformer_save_path, f"{flux_type}_trans_block_{idx}.pt"))

    class FluxSingleTransformerBlock(torch.nn.Module):
        def __init__(self, idx):
            super().__init__()
            self.block = model.single_transformer_blocks[idx]
        def forward(
            self,
            hidden_states,
            temb,
            image_rotary_emb,
        ):
            hidden_states = self.block(hidden_states, temb, image_rotary_emb)
            return hidden_states

    dummy_input = build_input(input_shapes[f'{flux_type}_single_trans_block'])
    for idx in range(38):
        traced_model = torch.jit.trace(FluxSingleTransformerBlock(idx), dummy_input)
        traced_model.save(os.path.join(dev_transformer_save_path if flux_type == "dev" else schnell_transformer_save_path, f"{flux_type}_single_trans_block_{idx}.pt"))

    class Tail(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.norm_out = model.norm_out
            self.proj_out = model.proj_out
        def forward(
            self,
            hidden_states,
            temb,
        ):
            hidden_states = self.norm_out(hidden_states, temb)
            output = self.proj_out(hidden_states)
            return output
    dummy_input = build_input(input_shapes[f'{flux_type}_tail'])
    traced_model = torch.jit.trace(Tail(), dummy_input)
    traced_model.save(os.path.join(dev_transformer_save_path if flux_type == "dev" else schnell_transformer_save_path, f"{flux_type}_tail.pt"))

def export_vae_decoder(model, use_taef1, input_shapes):
    eval_mode(model)
    vae_config_scaling_factor = 1 if use_taef1 else 0.3611
    vae_config_shift_factor = 1 if use_taef1 else 0.1159
    dummy_input = build_input(input_shapes['vae_decoder'])
    def build_vae_decoder(latents):
        with torch.no_grad():
            latents = (latents / vae_config_scaling_factor) + vae_config_shift_factor
            return model.decode(latents)[0]
    traced_model = torch.jit.trace(build_vae_decoder, dummy_input)
    traced_model.save(os.path.join(vae_decoder_model_save_path, "tiny_vae_decoder.pt" if use_taef1 else "vae_decoder.pt"))
    torch.onnx.export(traced_model, dummy_input, os.path.join(vae_decoder_model_save_path, "tiny_vae_decoder.onnx" if use_taef1 else "vae_decoder.onnx"))

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    # flux_type
    parser.add_argument("--flux_type", type=str, default="dev", help="dev or schnell")
    # use tiny vae
    parser.add_argument("--use_taef1", action="store_true", help="export tiny vae decoder when add '--use_taef1'")
    # image size
    parser.add_argument("--img_size", type=int, default=1024, help="generated image size, 512 or 1024")

    args = parser.parse_args()

    if args.flux_type == "dev":
        flux = FluxPipeline.from_pretrained("black-forest-labs/FLUX.1-dev", torch_dtype=data_type)
    elif args.flux_type == "schnell": 
        flux = FluxPipeline.from_pretrained("black-forest-labs/FLUX.1-schnell", torch_dtype=data_type)
    
    if args.use_taef1:
        from diffusers import AutoencoderTiny
        flux.vae = AutoencoderTiny.from_pretrained("madebyollin/taef1")

    if args.img_size == 1024:
        input_shapes = original_input_shapes
    elif args.img_size == 512:
        input_shapes = halved_input_shapes

    attention_processor.apply_rope = make_apply_rope(args.img_size)

    export_clip(flux.text_encoder, input_shapes['clip'])
    export_t5(flux.text_encoder_2, input_shapes['t5'])
    export_transformer(flux.transformer, args.flux_type, input_shapes[f'{args.flux_type}'])
    export_vae_decoder(flux.vae, args.use_taef1, input_shapes['vae'])