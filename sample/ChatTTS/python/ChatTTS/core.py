import os
import logging
import tempfile
from dataclasses import dataclass, asdict
from typing import Literal, Optional, List, Tuple, Dict, Union
from json import load
from pathlib import Path
import numpy as np
import torch
from .npuengine import EngineOV
from .model.vocos_spectral_ops import ISTFT
from .config import Config
from .model import DVAE, GPT, gen_logits, Tokenizer, Speaker
from .utils import (
    check_all_assets,
    download_all_assets,
    select_device,
    get_latest_modified_file,
    del_all,
)
from .utils import logger as utils_logger
from .norm import Normalizer

class Chat:
    def __init__(self, logger=logging.getLogger(__name__)):
        self.version = "1.0.0"
        self.logger = logger
        utils_logger.set_logger(logger)

        self.config = Config()

        self.normalizer = Normalizer(
            os.path.join(os.path.dirname(__file__), "res", "homophones_map.json"),
            logger,
        )
        with open(
            os.path.join(os.path.dirname(__file__), "res", "sha256_map.json")
        ) as f:
            self.sha256_map: Dict[str, str] = load(f)

        self.context = GPT.Context()

    def has_loaded(self, use_decoder=False):
        not_finish = False
        check_list = ["vocos", "gpt", "tokenizer"]

        if use_decoder:
            check_list.append("decoder")
        else:
            check_list.append("dvae")

        for module in check_list:
            if not hasattr(self, module):
                self.logger.warning(f"{module} not initialized.")
                not_finish = True

        return not not_finish

    def load(
        self,
        local_path='./model_files',
        coef=None,
        tpu_id: int = 0,
    ) -> bool:
        return self._load(
            tpu_id=tpu_id,
            coef=coef,
            **{
                k: os.path.join(local_path, v)
                for k, v in asdict(self.config.path).items()
            },
        )

    def unload(self):
        logger = self.logger
        self.normalizer.destroy()
        del self.normalizer
        del self.sha256_map
        del_list = ["vocos", "gpt", "decoder", "dvae", "tokenizer"]
        for module in del_list:
            if hasattr(self, module):
                delattr(self, module)
        self.__init__(logger)

    def sample_random_speaker(self) -> str:
        return self.speaker.sample_random()
    
    def sample_random_speaker_num(self) -> str:
        return self.speaker.sample_random_num()
    
    def sample_audio_speaker(self, wav: Union[np.ndarray, torch.Tensor]) -> str:
        return self.speaker.encode_prompt(self.dvae.sample_audio(wav))

    @dataclass(repr=False, eq=False)
    class RefineTextParams:
        prompt: str = ""
        top_P: float = 0.7
        top_K: int = 20
        temperature: float = 0.7
        repetition_penalty: float = 1.0
        max_new_token: int = 384
        min_new_token: int = 0
        show_tqdm: bool = True
        ensure_non_empty: bool = True
        manual_seed: Optional[int] = None

    @dataclass(repr=False, eq=False)
    class InferCodeParams(RefineTextParams):
        prompt: str = "[speed_5]"
        spk_emb: Optional[str] = None
        spk_smp: Optional[str] = None
        txt_smp: Optional[str] = None
        temperature: float = 0.3
        repetition_penalty: float = 1.05
        max_new_token: int = 2048
        stream_batch: int = 24
        stream_speed: int = 12000
        pass_first_n_batches: int = 2

    def infer(
        self,
        text,
        stream=False,
        lang=None,
        skip_refine_text=False,
        refine_text_only=False,
        use_decoder=True,
        do_text_normalization=True,
        do_homophone_replacement=True,
        params_refine_text=RefineTextParams(),
        params_infer_code=InferCodeParams(),
    ):
        self.context.set(False)
        res_gen = self._infer(
            text,
            stream,
            lang,
            skip_refine_text,
            refine_text_only,
            use_decoder,
            do_text_normalization,
            do_homophone_replacement,
            params_refine_text,
            params_infer_code,
        )
        if stream:
            return res_gen
        else:
            return next(res_gen)

    def interrupt(self):
        self.context.set(True)

    @torch.no_grad()
    def _load(
        self,
        vocos_ckpt_path: str = None,
        dvae_ckpt_path: str = None,
        gpt_ckpt_path: str = None,
        decoder_ckpt_path: str = None,
        tokenizer_path: str = None,
        coef: str = None,
        tpu_id: int = 0
    ):
        self.vocos = EngineOV(vocos_ckpt_path, batch=1, device_id=tpu_id)
        self.postprocess = ISTFT(n_fft=1024, hop_length=256, win_length=1024, padding='center')

        self.logger.log(logging.INFO, "vocos loaded.")

        dvae = (
            DVAE(
                decoder_config=asdict(self.config.dvae.decoder),
                encoder_config=asdict(self.config.dvae.encoder),
                vq_config=asdict(self.config.dvae.vq),
                dim=self.config.dvae.decoder.idim,
                coef=coef,
            )
            .to(torch.device("cpu"))
            .eval()
        )
        coef = str(dvae)
        assert dvae_ckpt_path, "dvae_ckpt_path should not be None"
        dvae.load_state_dict(torch.load(dvae_ckpt_path, weights_only=True), strict=False) #, mmap=True)
        self.dvae = dvae
        self.logger.log(logging.INFO, "dvae loaded.")

        gpt = GPT(
            model_path=gpt_ckpt_path,
            gpt_config=asdict(self.config.gpt),
            tpu_id=tpu_id,
            logger=self.logger,
        )
        self.gpt = gpt

        self.speaker = Speaker(
            self.config.gpt.hidden_size, self.config.spk_stat, torch.device("cpu")
        )
        self.logger.log(logging.INFO, "gpt loaded.")

        self.decoder = EngineOV(decoder_ckpt_path, batch=1, device_id=tpu_id)
        self.logger.log(logging.INFO, "decoder loaded.")

        if tokenizer_path:
            self.tokenizer = torch.load(tokenizer_path, map_location='cpu')
            self.tokenizer.padding_side = 'left'
            self.logger.log(logging.INFO, "tokenizer loaded.")

        self.coef = coef

        return self.has_loaded()

    def _infer(
        self,
        text,
        stream=False,
        lang=None,
        skip_refine_text=False,
        refine_text_only=False,
        use_decoder=True,
        do_text_normalization=True,
        do_homophone_replacement=True,
        params_refine_text=RefineTextParams(),
        params_infer_code=InferCodeParams(),
    ):

        assert self.has_loaded(use_decoder=use_decoder)

        if not isinstance(text, list):
            text = [text]

        assert len(text), 'text should not be empty'

        text = [
            self.normalizer(
                t,
                do_text_normalization,
                do_homophone_replacement,
                lang,
            )
            for t in text
        ]

        self.logger.debug("normed texts %s", str(text))

        if not skip_refine_text:
            refined = self._refine_text(
                text,
                params_refine_text,
            )
            text_tokens = refined.ids
            text_tokens = [i[i.less(self.tokenizer.convert_tokens_to_ids('[break_0]'))] for i in text_tokens]
            text = self.tokenizer.batch_decode(text_tokens)
            refined.destroy()
            if refine_text_only:
                yield text
                return
        print(text)

        if stream:
            length = 0
            pass_batch_count = 0
        for result in self._infer_code(
            text,
            stream,
            use_decoder,
            params_infer_code,
        ):
            wavs = self._decode_to_wavs(
                result.hiddens if use_decoder else result.ids,
                use_decoder,
            )
            result.destroy()
            if stream:
                pass_batch_count += 1
                if pass_batch_count <= params_infer_code.pass_first_n_batches:
                    continue
                a = length
                b = a + params_infer_code.stream_speed
                if b > wavs.shape[1]:
                    b = wavs.shape[1]
                new_wavs = wavs[:, a:b]
                length = b
                yield new_wavs
            else:
                yield wavs
        if stream and length < wavs.shape[1]:
            new_wavs = wavs[:, length:]
            yield new_wavs

    @torch.inference_mode()
    def _vocos_decode(self, spec: torch.Tensor) -> np.ndarray:
        mag, x, y = self.vocos([spec.numpy()])
        mag = torch.from_numpy(mag)
        x = torch.from_numpy(x)
        y = torch.from_numpy(y)
        S = mag * (x + 1j * y)
        audio = self.postprocess(S)
        return audio


    @torch.inference_mode()
    def _decode_to_wavs(
        self,
        result_list: List[torch.Tensor],
        use_decoder: bool,
    ):
        assert len(result_list) <= 1, "Now _decode_to_wavs only support one batch."
        max_x_len = self.decoder.input_shape[-1]
        real_len = max_x_len
        batch_result = torch.zeros(
            (len(result_list), result_list[0].size(1), max_x_len),
            dtype=result_list[0].dtype,
            device=result_list[0].device,
        )
        for i in range(len(result_list)):
            src = result_list[i]
            real_len = src.size(0)
            batch_result[i].narrow(1, 0, src.size(0)).copy_(src.permute(1, 0))
            del src
        del_all(result_list)
        if use_decoder:
            mel_specs = torch.from_numpy(self.decoder([batch_result.numpy()])[0])
            del batch_result
        else:
            mel_specs = self.dvae(batch_result)
            del batch_result
            # padding mel_specs to the same length [..., 2048]
            mel_specs = torch.nn.functional.pad(
                mel_specs,
                (0, max_x_len*2 - mel_specs.size(-1)),
                mode="constant",
                value=0,
            )
        wavs = self._vocos_decode(mel_specs)
        del mel_specs
        # clip wav to real length len*()
        wavs = wavs[:, :int(float(real_len/max_x_len)*wavs.size(-1))]

        return wavs

    @torch.no_grad()
    def _infer_code(
        self,
        text: Tuple[List[str], str],
        stream: bool,
        return_hidden: bool,
        params: InferCodeParams,
        device: torch.device=torch.device("cpu"),
    ):

        gpt = self.gpt

        if not isinstance(text, list):
            text = [text]

        assert len(text), "text should not be empty"

        if not isinstance(params.temperature, list):
            temperature = [params.temperature] * self.config.gpt.num_vq
        else:
            temperature = params.temperature

        input_ids = self.tokenizer(
            self.speaker.decorate_code_prompts(
                text,
                params.prompt,
                params.txt_smp,
                params.spk_emb,
            ),
            return_tensors='pt', 
            add_special_tokens=False, padding=True
        ).input_ids
        start_idx = input_ids.shape[-2]
        num_code = self.config.gpt.num_audio_tokens - 1

        logits_warpers, logits_processors = gen_logits(
            num_code=num_code,
            top_P=params.top_P,
            top_K=params.top_K,
            repetition_penalty=params.repetition_penalty,
        )
        result = gpt.generate(
            input_ids,
            temperature=torch.tensor(temperature, device=device),
            eos_token=num_code,
            # attention_mask=attention_mask,
            max_new_token=params.max_new_token,
            min_new_token=params.min_new_token,
            logits_processors=(*logits_processors, *logits_warpers),
            infer_text=False,
            spk_emb=params.spk_emb,
            return_hidden=return_hidden,
            stream=stream,
            show_tqdm=params.show_tqdm,
            ensure_non_empty=params.ensure_non_empty,
            stream_batch=params.stream_batch,
            manual_seed=params.manual_seed,
            context=self.context,
        )

        del input_ids

        return result

    @torch.no_grad()
    def _refine_text(
        self,
        text: str,
        params: RefineTextParams,
        device: torch.device=torch.device("cpu"),
    ):

        gpt = self.gpt
        input_ids = self.tokenizer(
            self.speaker.decorate_text_prompts(text, params.prompt),
            return_tensors='pt', 
            add_special_tokens=False, padding=True).input_ids

        logits_warpers, logits_processors = gen_logits(
            num_code=self.config.gpt.num_audio_tokens - 1,
            top_P=params.top_P,
            top_K=params.top_K,
            repetition_penalty=params.repetition_penalty,
        )

        result = next(
            gpt.generate(
                input_ids,
                temperature=torch.tensor([params.temperature], device=device),
                eos_token=21136,
                max_new_token=params.max_new_token,
                min_new_token=params.min_new_token,
                logits_processors=(*logits_processors, *logits_warpers),
                infer_text=True,
                stream=False,
                show_tqdm=params.show_tqdm,
                ensure_non_empty=params.ensure_non_empty,
                manual_seed=params.manual_seed,
                context=self.context,
            )
        )

        del input_ids

        return result