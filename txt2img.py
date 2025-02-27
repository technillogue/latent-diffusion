import argparse
import os
from typing import Any, Optional, Union
import logging
import traceback
import sys
from datetime import datetime
import numpy as np
import torch
from einops import rearrange
from omegaconf import OmegaConf
from PIL import Image
from torchvision.utils import make_grid
from tqdm import trange

from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.util import instantiate_from_config

# class BetterNamespace(argparse.Namespace):
#     def with_update(self, other: dict[str, Any]) -> "BetterNamespace":
#         new_namespace = BetterNamespace(**self.__dict__)
#         new_namespace.__dict__.update(other)


def mk_slug(text: Union[str, list[str]], time: str = "") -> str:
    "strip offending charecters"
    really_time = time if time else datetime.now().isoformat()
    text = really_time + "".join(text).encode("ascii", errors="ignore").decode()
    return (
        "".join(c if (c.isalnum() or c in "._") else "_" for c in text)[:200]
        + hex(hash(text))[-4:]
    )


def load_model_from_config(config, ckpt, verbose=True):
    print(f"Loading model from {ckpt}")
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    pl_sd = torch.load(ckpt, map_location=device)
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.to(device)
    model.eval()
    return model


def get_args(args: Optional[dict] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="a painting of a virus monster playing guitar",
        help="the prompt to render",
    )

    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="outputs/txt2img-samples",
    )
    parser.add_argument(
        "--ddim_steps",
        type=int,
        default=200,
        help="number of ddim sampling steps",
    )
    parser.add_argument(
        "--plms",
        action="store_false",
        help="use plms sampling",
    )
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=1,
        help="sample this often",
    )

    parser.add_argument(
        "--H",
        type=int,
        default=256,
        help="image height, in pixel space",
    )

    parser.add_argument(
        "--W",
        type=int,
        default=256,
        help="image width, in pixel space",
    )

    parser.add_argument(
        "--n_samples",
        type=int,
        default=1,
        help="how many samples to produce for the given prompt",
    )

    parser.add_argument(
        "--scale",
        type=float,
        default=5.0,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    if args:
        # check if the promt has dashes or something and parses okay
        # otherwise, treat it a full prompt
        # args["prompt"]
        fake_argv = [
            word for key, value in args.items() for word in [f"--{key}", str(value)]
        ]
        return parser.parse_known_args(fake_argv)[0]
    return parser.parse_known_args()[0]


def generate(model: Any, opt: argparse.Namespace) -> tuple[Any, str]:
    if not model:
        config = OmegaConf.load(
            "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
        )  # TODO: Optionally download from same location as ckpt and chnage this logic
        model = load_model_from_config(
            config, "models/ldm/text2img-large/model.ckpt"
        )  # TODO: check path

        device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        logging.info(device)
        model = model.to(device)

    if opt.plms:
        sampler = PLMSSampler(model)
    else:
        sampler = DDIMSampler(model)

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    prompt = opt.prompt

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))

    all_samples = []
    with torch.no_grad():
        with model.ema_scope():
            uc = None
            if opt.scale != 1.0:
                uc = model.get_learned_conditioning(opt.n_samples * [""])
            for n in trange(opt.n_iter, desc="Sampling"):
                logging.info(n)
                try:
                    logging.info(torch.cuda.memory_stats(torch.device("cuda:0")))
                except:  # pylint: disable=bare-except
                    exception_traceback = "".join(
                        traceback.format_exception(*sys.exc_info())
                    )
                    logging.info(exception_traceback)
                c = model.get_learned_conditioning(opt.n_samples * [prompt])
                shape = [4, opt.H // 8, opt.W // 8]
                samples_ddim, _ = sampler.sample(
                    S=opt.ddim_steps,
                    conditioning=c,
                    batch_size=opt.n_samples,
                    shape=shape,
                    verbose=False,
                    unconditional_guidance_scale=opt.scale,
                    unconditional_conditioning=uc,
                    eta=opt.ddim_eta,
                )

                x_samples_ddim = model.decode_first_stage(samples_ddim)
                x_samples_ddim = torch.clamp(
                    (x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0
                )

                for x_sample in x_samples_ddim:
                    x_sample = 255.0 * rearrange(
                        x_sample.cpu().numpy(), "c h w -> h w c"
                    )
                    Image.fromarray(x_sample.astype(np.uint8)).save(
                        os.path.join(sample_path, f"{base_count:04}.png")
                    )
                    base_count += 1
                all_samples.append(x_samples_ddim)

    # additionally, save as grid
    grid = torch.stack(all_samples, 0)
    grid = rearrange(grid, "n b c h w -> (n b) c h w")
    grid = make_grid(grid, nrow=opt.n_samples)

    # to image
    grid = 255.0 * rearrange(grid, "c h w -> h w c").cpu().numpy()
    output_path = os.path.join(outpath, mk_slug(prompt) + ".png")
    Image.fromarray(grid.astype(np.uint8)).save(output_path)

    print(f"Your samples are ready and waiting four you here: \n{outpath} \nEnjoy.")
    return model, output_path


if __name__ == "__main__":
    generate(None, get_args())
