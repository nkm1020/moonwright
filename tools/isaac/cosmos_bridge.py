"""The rover's perception: NVIDIA Cosmos Reason 2 looks at one navigation-camera image and says what stops it.

mission.py runs this when its health check finds a rover that is commanded to drive but makes no
headway. Cosmos proposes the cause and physics checks it: "blocked" -> the rover is taken to be sound
and goes around (a rover that stalls again on its way around is lost after all); anything else -> the
rover is lost and Moonwright replans without it. It runs in its own venv (torch with CUDA,
transformers with Qwen3-VL, bitsandbytes), apart from Isaac Sim's Python:

    python -m venv cosmos && cosmos\\Scripts\\pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
    cosmos\\Scripts\\pip install "transformers>=4.57" accelerate pillow "bitsandbytes>=0.45"
    set HF_TOKEN=...   (the Cosmos Reason 2 models are gated: accept each one's terms on Hugging Face first)
    cosmos\\Scripts\\python cosmos_bridge.py front.png answer.json   (COSMOS_MODEL=nvidia/Cosmos-Reason2-2B for 2B)

The model is `nvidia/Cosmos-Reason2-8B` (NVIDIA Open Model License) in 4 bits (NF4) on the local GPU,
7.1 GB at its peak beside Isaac Sim on a 12 GB card: the size a robot's own computer could carry. On
ten held-out camera pictures (cosmos_eval.py) it found 4 of 5 rocks, the fifth out of its camera's
sight, and cleared all 5 drive faults; the 2B, in bfloat16 at 5.2 GB, cleared the faults too but
found 1 rock of 5, taking the lamp-lit rock for flat ground. The prompt follows the card: the default
system prompt, the <think> reasoning format, its own sampling (generation_config.json: temperature
0.7, top-p 0.8, top-k 20; greedy decoding can loop: one run thought for 4,096 tokens and never
answered); the final answer is one JSON object: blocked, cause, can_continue, evidence.

The question is about sight only: is there something at the rover's nose it cannot drive over. It is
not told that the rover has stalled (mission.py knows that, and puts the two together): told, both
models reasoned "it has not moved, so something must be in the way" and called clear roads blocked.
It carries what the rover knows of itself: the camera's height, its own nose in the frame, its wheels,
and where the sun is, so that its own long shadow ahead is not taken for a rock (it was). Up to five
answers vote (self-consistency), one at a time with seeds 0, 1, 2, ..., until three agree; an answer
still thinking after 2,048 tokens, or with no JSON in it, abstains. (Five at once in one batch filled
the 12 GB card and ran for an hour.) Seeded, so a run repeats; every answer is kept."""
import argparse
import json
import os
import re
import time

import torch
import transformers
from PIL import Image

MODEL = os.environ.get("COSMOS_MODEL", "nvidia/Cosmos-Reason2-8B")   # or nvidia/Cosmos-Reason2-2B
BITS4 = "8B" in MODEL   # 8B in bfloat16 is 17 GB; in 4 bits (NF4) it fits the 12 GB card beside Isaac Sim
SEED, VOTES, MAX_NEW = 0, 5, 2048
SYSTEM = "You are a helpful assistant."   # the card's own system prompt
CAMERA = ("The camera is on a mast 2 m above the ground, looking ahead and down; the flat grey shape at the "
          "bottom of each image is the rover's own nose. Its wheels are 0.8 m across. The sun is low behind the "
          "rover: the rover's own shadow is the long dark wedge reaching from its nose toward the horizon, and "
          "other dark patches on the ground are shadows too. ")
ASK = ("Look at the ground directly in front of the rover's nose, within about a metre of it, in the image taken "
       "now. Is there an object there that the rover cannot drive over, such as a rock taller than its wheels' "
       "radius? Ignore anything farther away. Answer the question using the following format:\n\n<think>\n"
       "Your reasoning.\n</think>\n\nWrite your final answer immediately after the </think> tag, as one JSON "
       'object: {"blocked": true or false, "cause": "a short phrase", "can_continue": true or false, '
       '"evidence": "what in the image shows it"}')
QUESTION = (   # word for word the question cosmos_eval.py measured (it once also took an earlier frame)
    "This image is from the navigation camera of a wheeled construction rover working on the Moon. " + CAMERA + ASK)


def load():
    quant = transformers.BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                            bnb_4bit_compute_dtype=torch.bfloat16) if BITS4 else None
    model = transformers.Qwen3VLForConditionalGeneration.from_pretrained(
        MODEL, dtype=torch.bfloat16, device_map="cuda", attn_implementation="sdpa", quantization_config=quant)
    return model, transformers.AutoProcessor.from_pretrained(MODEL)


def ask(model, processor, img_path):
    """up to five sampled answers about one camera image, until three agree, and their vote"""
    messages = [
        {"role": "system", "content": [{"type": "text", "text": SYSTEM}]},
        {"role": "user", "content": [{"type": "image", "image": Image.open(img_path).convert("RGB")},
                                     {"type": "text", "text": QUESTION}]},
    ]
    inputs = processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                           return_dict=True, return_tensors="pt").to(model.device)
    n_in = inputs.input_ids.shape[1]
    samples, t1 = [], time.perf_counter()
    for k in range(VOTES):
        transformers.set_seed(SEED + k)
        t2 = time.perf_counter()
        with torch.inference_mode():   # the card's sampling settings
            ids = model.generate(**inputs, max_new_tokens=MAX_NEW)
        text = processor.decode(ids[0][n_in:], skip_special_tokens=True, clean_up_tokenization_spaces=False)
        m = re.search(r"\{.*\}", text.split("</think>")[-1], re.DOTALL) if "</think>" in text else None
        try:
            ans = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            ans = None
        samples.append({"seed": SEED + k, "raw": text, "answer": ans if isinstance(ans, dict) else None,
                        "new_tokens": int(ids.shape[1] - n_in), "generate_s": round(time.perf_counter() - t2, 1)})
        yes = sum(1 for x in samples if x["answer"] and x["answer"].get("blocked") is True)
        no = sum(1 for x in samples if x["answer"] and x["answer"].get("blocked") is not True)
        if max(yes, no) > VOTES // 2:
            break
    gen_s = time.perf_counter() - t1
    said = [x["answer"].get("blocked") is True for x in samples if x["answer"]]
    blocked = sum(said) >= (VOTES // 2 + 1)
    pick = next((x["answer"] for x in samples if x["answer"] and (x["answer"].get("blocked") is True) == blocked), None)
    return {"model": MODEL + (" (4-bit NF4)" if BITS4 else ""), "question": QUESTION, "answer": dict(pick or {}, blocked=blocked) if said else None,
            "votes": {"blocked": sum(said), "clear": len(said) - sum(said), "abstained": len(samples) - len(said)},
            "seed": SEED, "generate_s": round(gen_s, 1), "gpu": torch.cuda.get_device_name(0),
            "peak_gpu_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2), "samples": samples}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image"); ap.add_argument("out")
    a = ap.parse_args()
    t0 = time.perf_counter()
    model, processor = load()
    load_s = time.perf_counter() - t0
    rec = ask(model, processor, a.image)
    rec["load_s"] = round(load_s, 1)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    print(f"Cosmos Reason 2: {rec['answer']} (votes {rec['votes']}, {rec['generate_s']} s, "
          f"{rec['peak_gpu_gb']} GB peak)", flush=True)
    return 0 if rec["answer"] is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
