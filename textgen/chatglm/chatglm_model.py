# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description:
"""
import os
import random
import re
import sys
from typing import List, Tuple

import numpy as np
import torch
import torch.nn as nn
from loguru import logger
from peft import (
    get_peft_model,
    LoraConfig,
    TaskType,
    PeftModel,
    prepare_model_for_int8_training,
    set_peft_model_state_dict,
)
from tqdm.auto import tqdm
from transformers import Trainer, TrainingArguments, AutoTokenizer, AutoModel, AutoConfig
from transformers.trainer import TRAINING_ARGS_NAME

from textgen.chatglm.chatglm_utils import load_hf_dataset, ChatGlmDataset
from textgen.config.model_args import ChatGlmArgs

has_cuda = torch.cuda.is_available()
os.environ["TOKENIZERS_PARALLELISM"] = "FALSE"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

MODEL_CLASSES = {
    "chatglm": (AutoConfig, AutoModel, AutoTokenizer),
}


class ChatGlmModel:
    def __init__(
            self,
            model_type,
            model_name,
            lora_name=None,
            args=None,
            use_cuda=has_cuda,
            cuda_device=-1,
            **kwargs,
    ):

        """
        Initializes a ChatGLMModel model.

        Args:
            model_type: The type of model (chatglm)
            model_name: The exact architecture and trained weights to use. This may be a Hugging Face Transformers compatible pre-trained model, a community model, or the path to a directory containing model files.
            lora_name (optional): Lora name
            args (optional): Default args will be used if this parameter is not provided. If provided, it should be a dict containing the args that should be changed in the default args.
            use_cuda (optional): Use GPU if available. Setting to False will force model to use CPU only.
            cuda_device (int, optional): Specific GPU that should be used. Will use the first available GPU by default.
            **kwargs (optional): For providing proxies, force_download, resume_download, cache_dir and other options specific to the 'from_pretrained' implementation where this will be supplied.
        """  # noqa: ignore flake8"
        model_type = model_type.lower()
        self.args = self._load_model_args(model_name)

        if isinstance(args, dict):
            self.args.update_from_dict(args)
        elif isinstance(args, ChatGlmArgs):
            self.args = args
        if self.args.manual_seed:
            random.seed(self.args.manual_seed)
            np.random.seed(self.args.manual_seed)
            torch.manual_seed(self.args.manual_seed)
            if self.args.n_gpu > 0:
                torch.cuda.manual_seed_all(self.args.manual_seed)

        if use_cuda:
            if torch.cuda.is_available():
                if cuda_device == -1:
                    self.device = torch.device("cuda")
                else:
                    self.device = torch.device(f"cuda:{cuda_device}")
            else:
                raise ValueError(
                    "'use_cuda' set to True when cuda is unavailable."
                    "Make sure CUDA is available or set `use_cuda=False`."
                )
        else:
            self.device = "cpu"
        logger.debug(f"Device: {self.device}")
        if self.device == "cpu":
            self.args.fp16 = False
            self.args.int8 = False

        self.results = {}
        config_class, model_class, tokenizer_class = MODEL_CLASSES[model_type]
        if model_name is None:
            model_name = self.args.model_name_or_path
        config = AutoConfig.from_pretrained(model_name, trust_remote_code=True, **kwargs)

        self.model = model_class.from_pretrained(
            model_name,
            config=config,
            trust_remote_code=True,
            load_in_8bit=self.args.int8,
            torch_dtype=torch.float16 if self.args.fp16 else torch.float32,
        )
        self.model.to(self.device)

        if self.args.quantization_bit:
            logger.debug(f"Quantized to {self.args.quantization_bit} bit")
            self.model = self.model.quantize(self.args.quantization_bit)
        self.tokenizer_class = tokenizer_class
        if self.args.tokenizer_name:
            self.tokenizer = tokenizer_class.from_pretrained(self.args.tokenizer_name, trust_remote_code=True)
        else:
            self.tokenizer = tokenizer_class.from_pretrained(model_name, trust_remote_code=True)
            self.args.tokenizer_name = self.args.model_name

        self.args.model_type = model_type
        if model_name is None:
            self.args.model_name = "ChatGLM_from_scratch"
        else:
            self.args.model_name = model_name

        self.lora_name = lora_name
        if self.args.use_lora:
            self.load_lora()

    def data_collator(self, batch):
        """Data collator that will dynamically pad the inputs received."""
        len_ids = [len(example) for example in batch]
        longest = max(len_ids)
        input_ids = []
        labels_list = []
        for ids_l, example in sorted(zip(len_ids, batch), key=lambda x: -x[0]):
            ids = list(example)
            seq_len = ids.index(self.tokenizer.bos_token_id) + 1  # is equal to prompt length
            labels = ([-100] * (seq_len - 1) + ids[(seq_len - 1):] + [-100] * (longest - ids_l))
            ids = ids + [self.tokenizer.pad_token_id] * (longest - ids_l)
            _ids = torch.LongTensor(ids)
            labels_list.append(torch.LongTensor(labels))
            input_ids.append(_ids)
        input_ids = torch.stack(input_ids)
        labels = torch.stack(labels_list)
        return {"input_ids": input_ids, "labels": labels}

    def train_model(
            self,
            train_data,
            output_dir=None,
            args=None,
            eval_data=None,
            verbose=True,
            **kwargs,
    ):
        """
        Trains the model using 'train_data'

        Args:
            train_data: Pandas DataFrame containing the 3 columns - `instruction`, `input`, `output`.
                        - `instruction`: The instruction text. (E.g. `"correct the following:"`)
                        - `input`: The input text sequence. `instruction` is automatically prepended to form the full input. (<instruction> `\n` <input>)
                        - `output`: The target sequence
            output_dir: The directory where model files will be saved. If not given, self.args.output_dir will be used.
            args (optional): Optional changes to the args dict of the model. Any changes made will persist for the model.
            eval_data (optional): A DataFrame against which evaluation will be performed when evaluate_during_training is enabled. Is required if evaluate_during_training is enabled.
            verbose (optional): If True, all of the warnings related to data processing will be printed. 
            **kwargs: Additional metrics that should be used. Pass in the metrics as keyword arguments (name of metric: function to use).
                        A metric function should take in two parameters. The first parameter will be the true labels, and the second parameter will be the predictions. Both inputs
                        will be lists of strings. Note that this will slow down training significantly as the predicted sequences need to be generated.

        Returns:
            global_step: Number of global steps trained
            training_details: Average training loss if evaluate_during_training is False or full training progress scores if evaluate_during_training is True
        """  # noqa: ignore flake8"

        if args:
            self.args.update_from_dict(args)
        if self.args.evaluate_during_training and eval_data is None:
            raise ValueError(
                "evaluate_during_training is enabled but eval_data is not specified."
                " Pass eval_data to model.train_model() if using evaluate_during_training."
            )

        if not output_dir:
            output_dir = self.args.output_dir
        if (
                os.path.exists(output_dir)
                and os.listdir(output_dir)
                and not self.args.overwrite_output_dir
        ):
            raise ValueError(
                "Output directory ({}) already exists and is not empty."
                " Set args.overwrite_output_dir = True to overcome.".format(output_dir)
            )
        # update model train config
        self.model.gradient_checkpointing_enable()
        self.model.enable_input_require_grads()
        if torch.cuda.device_count() > 1:
            self.model.is_parallelizable = True
            self.model.model_parallel = True
        self.model.lm_head = CastOutputToFloat(self.model.lm_head)
        self.model.config.use_cache = False
        resume_from_checkpoint = self.args.resume_from_checkpoint

        # setup peft, add lora config
        if self.args.use_lora:
            peft_config = LoraConfig(
                task_type=TaskType.CAUSAL_LM,
                inference_mode=False,
                r=self.args.lora_r,
                lora_alpha=self.args.lora_alpha,
                lora_dropout=self.args.lora_dropout,
                target_modules=self.args.lora_target_modules,
                bias=self.args.lora_bias,
            )
            if self.args.int8:
                self.model = prepare_model_for_int8_training(self.model)
            self.model = get_peft_model(self.model, peft_config)

            if resume_from_checkpoint:
                # Check the available weights and load them
                checkpoint_name = os.path.join(resume_from_checkpoint, "pytorch_model.bin")  # Full checkpoint
                if not os.path.exists(checkpoint_name):
                    checkpoint_name = os.path.join(
                        resume_from_checkpoint, "adapter_model.bin")  # only LoRA model - LoRA config above has to fit
                    resume_from_checkpoint = (
                        False  # So the trainer won't try loading its state
                    )
                # The two files above have a different name depending on how they were saved, but are actually the same.
                if os.path.exists(checkpoint_name):
                    logger.info(f"Restarting from {checkpoint_name}")
                    adapters_weights = torch.load(checkpoint_name)
                    set_peft_model_state_dict(self.model, adapters_weights)
                else:
                    logger.warning(f"Checkpoint {checkpoint_name} not found")

            self.model.print_trainable_parameters()  # Be more transparent about the % of trainable params.
        else:
            logger.warning("Now full model params fine-tune, which is slow, set `use_lora=True` for lora fine-tune.")
        os.makedirs(output_dir, exist_ok=True)

        # load dataset
        train_dataset = self.load_and_cache_examples(train_data)
        if verbose:
            logger.debug(f"train_dataset len: {len(train_dataset)}, train_dataset[0]: {train_dataset[0]}")
        eval_dataset = None
        if eval_data is not None:
            eval_dataset = self.load_and_cache_examples(eval_data, evaluate=True)
            if verbose:
                logger.debug(f"eval_dataset len: {len(eval_dataset)}, eval_dataset[0]: {eval_dataset[0]}")

        # start train
        training_args = TrainingArguments(
            output_dir=self.args.output_dir,
            learning_rate=self.args.learning_rate,
            num_train_epochs=self.args.num_train_epochs,
            logging_dir=f"{self.args.output_dir}/logs",
            logging_steps=self.args.logging_steps,
            max_steps=self.args.max_steps,
            per_device_train_batch_size=self.args.per_device_train_batch_size,
            per_device_eval_batch_size=self.args.per_device_train_batch_size,
            gradient_accumulation_steps=self.args.gradient_accumulation_steps,
            warmup_steps=self.args.warmup_steps,
            save_steps=self.args.save_steps,
            optim=self.args.optimizer,
            save_strategy=self.args.save_strategy,
            evaluation_strategy=self.args.evaluation_strategy,
            eval_steps=self.args.eval_steps,
            save_total_limit=self.args.save_total_limit,
            fp16=self.args.fp16,
            remove_unused_columns=self.args.remove_unused_columns,
            report_to=self.args.report_to,
            overwrite_output_dir=self.args.overwrite_output_dir,
            no_cuda=True if self.device == "cpu" else False,
            **kwargs
        )
        # Log on each process the small summary:
        logger.warning(
            f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}, "
            + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
        )
        logger.info(f"Training/evaluation parameters {training_args}")

        trainer = FinetuneTrainer(
            model=self.model,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset if eval_data is not None else None,
            args=training_args,
            tokenizer=self.tokenizer,
            data_collator=self.data_collator,
        )

        if self.args.enable_torch_compile:
            if torch.__version__ >= "2" and sys.platform != "win32":
                self.model = torch.compile(self.model)

        logger.info("*** Train ***")
        (global_step, training_loss, metrics) = trainer.train(resume_from_checkpoint=resume_from_checkpoint)
        self.handle_metrics("train", metrics, self.args.output_dir)
        self.results.update(metrics)
        self.save_model(model=self.model)

        if verbose:
            logger.debug(f"metrics: {self.results}")
            logger.info(
                " Training of {} model complete. Saved to {}.".format(
                    self.args.model_name, output_dir
                )
            )
        return global_step, training_loss

    @staticmethod
    def handle_metrics(split, metrics, output_dir):
        """
        Log and save metrics

        Args:
        - split: one of train, val, test
        - metrics: metrics dict
        - output_dir: where to save the metrics
        """

        logger.info(f"***** {split} metrics *****")
        for key in sorted(metrics.keys()):
            logger.info(f"  {key} = {metrics[key]}")
        output_file = os.path.join(output_dir, f"{split}_results.txt")
        with open(output_file, "w") as writer:
            for key in sorted(metrics.keys()):
                writer.write("{} = {}\n".format(key, str(metrics[key])))

    def load_lora(self):
        """Load lora model."""
        if self.lora_name:
            self.model = PeftModel.from_pretrained(
                self.model,
                self.lora_name,
                torch_dtype=torch.float16 if self.args.fp16 else torch.float32,
            )
            logger.info(f"Loaded lora model from {self.lora_name}")
        else:
            lora_path = os.path.join(self.args.output_dir, self.args.lora_bin_name)
            if lora_path and os.path.exists(lora_path):
                self.model = PeftModel.from_pretrained(
                    self.model,
                    self.args.output_dir,
                    torch_dtype=torch.float16 if self.args.fp16 else torch.float32,
                )
                logger.info(f"Loaded lora model from {lora_path}")

    def process_response(self, response):
        """Process response text."""
        response = response.strip()
        punkts = [
            [",", "，"],
            ["!", "！"],
            [":", "："],
            [";", "；"],
            ["\\?", "？"],
        ]
        for item in punkts:
            response = re.sub(r"([\u4e00-\u9fff])%s" % item[0], r"\1%s" % item[1], response)
            response = re.sub(r"%s([\u4e00-\u9fff])" % item[0], r"%s\1" % item[1], response)
        return response

    @torch.no_grad()
    def predict(self, sentences: List[str], keep_prompt: bool = False, max_length: int = None, **kwargs):
        """
        Performs predictions on a list of text.

        Args:
            sentences: A python list of text (str) to be sent to the model for prediction. 
            keep_prompt: Whether to keep the prompt in the generated text.
            max_length: The maximum length of the generated text.

        Returns:
            preds: A python list of the generated sequences.
        """  # noqa: ignore flake8"

        if self.device == 'cpu':
            self.model.float()
        if self.args.fp16:
            self.model.half()
        self.model.to(self.device)
        self.model.eval()

        all_outputs = []
        # Batching
        for batch in tqdm(
                [
                    sentences[i: i + self.args.eval_batch_size]
                    for i in range(0, len(sentences), self.args.eval_batch_size)
                ],
                desc="Generating outputs",
                disable=self.args.silent,
        ):
            inputs = self.tokenizer(batch, padding=True, return_tensors='pt').to(self.device)
            gen_kwargs = {
                "max_new_tokens": max_length if max_length else self.args.max_length,
                "num_beams": self.args.num_beams,
                "do_sample": self.args.do_sample,
                "top_p": self.args.top_p,
                "temperature": self.args.temperature,
                "eos_token_id": self.tokenizer.eos_token_id,
                **kwargs
            }
            outputs = self.model.generate(**inputs, **gen_kwargs)
            for idx, (prompt_text, generated_sequence) in enumerate(zip(batch, outputs)):
                # Decode text
                text = self.tokenizer.decode(generated_sequence)
                prompt_len = len(prompt_text)
                gen_text = text[prompt_len:]
                gen_text = self.process_response(gen_text)
                if keep_prompt:
                    total_sequence = prompt_text + gen_text
                else:
                    total_sequence = gen_text
                all_outputs.append(total_sequence)
        return all_outputs

    @torch.no_grad()
    def chat(self, query: str, history: List[Tuple[str, str]] = None,
             keep_prompt: bool = False, max_length: int = 128, **kwargs):
        """
        Chat with the model
        :param query:
        :param history:
        :param keep_prompt:
        :param max_length:
        :param kwargs:
        :return: response, history
        """
        if history is None:
            history = []
        if not history:
            prompt = query
        else:
            prompt = ""
            for i, (old_query, response) in enumerate(history):
                prompt += "[Round {}]\n问：{}\n答：{}\n".format(i, old_query, response)
            prompt += "[Round {}]\n问：{}\n答：".format(len(history), query)
        response = self.predict([prompt], keep_prompt=keep_prompt, max_length=len(prompt) + max_length, **kwargs)[0]
        history = history + [(query, response)]
        return response, history

    def load_and_cache_examples(
            self, data, evaluate=False, no_cache=False, verbose=True, silent=False
    ):
        """
        Creates a ChatGLMDataset from data.

        Utility function for train() and eval() methods. Not intended to be used directly.
        """

        tokenizer = self.tokenizer
        args = self.args

        if not no_cache:
            no_cache = args.no_cache

        if not no_cache:
            os.makedirs(self.args.cache_dir, exist_ok=True)

        mode = "dev" if evaluate else "train"

        if self.args.use_hf_datasets:
            dataset = load_hf_dataset(data, tokenizer, self.args, mode)
            return dataset
        elif args.dataset_class:
            CustomDataset = args.dataset_class
            return CustomDataset(tokenizer, args, data, mode)
        else:
            return ChatGlmDataset(
                tokenizer,
                self.args,
                data,
                mode,
            )

    def save_model(
            self, output_dir=None, optimizer=None, scheduler=None, model=None, results=None
    ):
        """Save the model and the tokenizer to the `output_dir`."""
        if not output_dir:
            output_dir = self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)

        if model and not self.args.no_save:
            # Take care of distributed/parallel training
            model_to_save = model.module if hasattr(model, "module") else model
            model_to_save.save_pretrained(output_dir)
            self.tokenizer.save_pretrained(output_dir)
            torch.save(self.args, os.path.join(output_dir, "training_args.bin"))
            if optimizer and scheduler and self.args.save_optimizer_and_scheduler:
                torch.save(
                    optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt")
                )
                torch.save(
                    scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt")
                )
            # save model
            self.save_model_args(output_dir)

    def save_model_args(self, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        self.args.save(output_dir)

    def _load_model_args(self, input_dir):
        args = ChatGlmArgs()
        args.load(input_dir)
        return args


class FinetuneTrainer(Trainer):
    """Finetune trainer for ChatGlmModel"""

    def compute_loss(self, model, inputs, return_outputs=False):
        """Computes the loss."""
        return model(
            input_ids=inputs["input_ids"],
            labels=inputs["labels"],
        ).loss

    def save_model(self, output_dir=None, _internal_call=False):
        """Save the LoRA model"""
        os.makedirs(output_dir, exist_ok=True)
        torch.save(self.args, os.path.join(output_dir, TRAINING_ARGS_NAME))
        self.model.save_pretrained(output_dir)


class CastOutputToFloat(nn.Sequential):
    """Cast the output of the model to float"""

    def forward(self, x):
        return super().forward(x).to(torch.float32)
