# coding=utf-8
# Copyright 2020 The Facebook AI Research Team Authors and The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Original implementation: https://github.com/pytorch/fairseq/tree/master/examples/wmt19
# Authors:
# - @alexeib Alexei Baevski
# - @edunov Sergey Edunov
# - @michaelauli Michael Auli
# - @myleott Myle Ott
# - @nng555 Nathan Ng
# - David Grangier
# - Kyra Yee
#
# Paper: Facebook FAIR's WMT19 News Translation Task Submission https://huggingface.co/papers/1907.06616
#
"""PyTorch Fairseq model, ported from https://github.com/pytorch/fairseq/tree/master/examples/wmt19"""

import math
from typing import Any, Optional, Union

import torch
from torch import Tensor, nn
from torch.nn import CrossEntropyLoss, LayerNorm

from ...activations import ACT2FN
from ...cache_utils import Cache, EncoderDecoderCache
from ...generation import GenerationMixin
from ...integrations.deepspeed import is_deepspeed_zero3_enabled
from ...modeling_outputs import (
    BaseModelOutput,
    BaseModelOutputWithPastAndCrossAttentions,
    Seq2SeqLMOutput,
    Seq2SeqModelOutput,
)
from ...modeling_utils import PreTrainedModel
from ...utils import auto_docstring, logging
from .configuration_fsmt import FSMTConfig


logger = logging.get_logger(__name__)


# See all FSMT models at https://huggingface.co/models?filter=fsmt

# Porting notes:
# this one is modeled after BartModel*
#
# Currently only translation (fairseq also has weights for LM)
#
# fairseq provides weights for ru-en, en-ru and de-en, en-de pairs. All have been ported.
# - ru-en, en-ru use asymmetric vocab
# - de-en, en-de use a merged single vocab (but the code works as if they are separate)
#
# Differences with Bart:
# - not using bos token
# - 2 separate vocabs (src and target)
# - embed weights aren't tied
# - uses a model Ensemble (but that part isn't ported/implemented yet) - so we
#   aren't getting as good of a BLEU score
# - uses a projection layer at the end of the decoder
# - doesn't use final_logits_bias
# - beam search: stops as soon as num_beams == len(hypos) (whereas transformers
#   is not satisfied there and will continue searching until the next cycles
#   aren't promising something better), comparing BLEU scores - the transformers
#   algorithm is slightly superior, therefore using the latter. But if you want
#   to match fairseq outputs, you need to pass ``early_stopping=True`` to ``generate()``.
#
# SinusoidalPositionalEmbedding is slightly different from Bart's - generates
# different embeddings. This implementation is copied verbatim from fairseq with
# some small changes to make it work here.
#
# Other changes:
#  - doesn't support use_cache as Bart's version does
#
#
# FSMTConfig changes with BartConfig
#
#    Differences with BART:
#    - src/tgt vocabs aren't shared
#    - token embeddings aren't shared
#    - needs a language pair
#    - scale_embedding are True
#
#    some unused args were removed too
#
#
# TODO:
# - port model ensemble (fs uses 4 model checkpoints)
# - solve beam search discrepancies
# docstyle-ignore

"""

Here is how to compare BLEU scores against fairseq implementation:
(don't forget to install sacrebleu: `pip install sacrebleu`)

# en-ru

export PAIR=en-ru
export DATA_DIR=data/$PAIR
export SAVE_DIR=data/$PAIR
export BS=8
export NUM_BEAMS=50
mkdir -p $DATA_DIR
sacrebleu -t wmt19 -l $PAIR --echo src > $DATA_DIR/val.source
sacrebleu -t wmt19 -l $PAIR --echo ref > $DATA_DIR/val.target
echo $PAIR
PYTHONPATH="src:examples/seq2seq" python examples/seq2seq/run_eval.py facebook/wmt19-$PAIR $DATA_DIR/val.source $SAVE_DIR/test_translations.txt --reference_path $DATA_DIR/val.target --score_path $SAVE_DIR/test_bleu.json --bs $BS --task translation --num_beams $NUM_BEAMS

# (fairseq BLEU: 36.4 http://matrix.statmt.org/matrix/output/1914?score_id=37605)


# ru-en

export PAIR=ru-en
export DATA_DIR=data/$PAIR
export SAVE_DIR=data/$PAIR
export BS=8
export NUM_BEAMS=50
mkdir -p $DATA_DIR
sacrebleu -t wmt19 -l $PAIR --echo src > $DATA_DIR/val.source
sacrebleu -t wmt19 -l $PAIR --echo ref > $DATA_DIR/val.target
PYTHONPATH="src:examples/seq2seq" python examples/seq2seq/run_eval.py facebook/wmt19-$PAIR $DATA_DIR/val.source $SAVE_DIR/test_translations.txt --reference_path $DATA_DIR/val.target --score_path $SAVE_DIR/test_bleu.json --bs $BS --task translation --num_beams $NUM_BEAMS


# (fairseq BLEU: 41.3 http://matrix.statmt.org/matrix/output/1907?run_id=6937)


# de-en

export PAIR=de-en
export DATA_DIR=data/$PAIR
export SAVE_DIR=data/$PAIR
export BS=8
export NUM_BEAMS=50
mkdir -p $DATA_DIR
sacrebleu -t wmt19 -l $PAIR --echo src > $DATA_DIR/val.source
sacrebleu -t wmt19 -l $PAIR --echo ref > $DATA_DIR/val.target
echo $PAIR
PYTHONPATH="src:examples/seq2seq" python examples/seq2seq/run_eval.py facebook/wmt19-$PAIR $DATA_DIR/val.source $SAVE_DIR/test_translations.txt --reference_path $DATA_DIR/val.target --score_path $SAVE_DIR/test_bleu.json --bs $BS --task translation --num_beams $NUM_BEAMS

# (fairseq BLEU: 42.3 http://matrix.statmt.org/matrix/output/1902?run_id=6750)



# en-de

export PAIR=en-de
export DATA_DIR=data/$PAIR
export SAVE_DIR=data/$PAIR
export BS=8
mkdir -p $DATA_DIR
sacrebleu -t wmt19 -l $PAIR --echo src > $DATA_DIR/val.source
sacrebleu -t wmt19 -l $PAIR --echo ref > $DATA_DIR/val.target
echo $PAIR
PYTHONPATH="src:examples/seq2seq" python examples/seq2seq/run_eval.py facebook/wmt19-$PAIR $DATA_DIR/val.source $SAVE_DIR/test_translations.txt --reference_path $DATA_DIR/val.target --score_path $SAVE_DIR/test_bleu.json --bs $BS --task translation --num_beams $NUM_BEAMS

# (fairseq BLEU: 43.1 http://matrix.statmt.org/matrix/output/1909?run_id=6862)

"""


def invert_mask(attention_mask):
    """Turns 1->0, 0->1, False->True, True-> False"""
    assert attention_mask.dim() == 2
    return attention_mask.eq(0)


def triu_onnx(x, diagonal=0):
    l = x.shape[0]
    arange = torch.arange(l, device=x.device)
    mask = arange.expand(l, l)
    arange = arange.unsqueeze(-1)
    if diagonal:
        arange = arange + diagonal
    mask = mask >= arange
    return x.masked_fill(mask == 0, 0)


def _prepare_fsmt_decoder_inputs(
    config,
    input_ids,
    decoder_input_ids=None,
    decoder_padding_mask=None,
    causal_mask_dtype=torch.float32,
):
    """
    Prepare masks that ignore padding tokens in the decoder and a causal mask for the decoder if none are provided.
    This mimics the default behavior in fairseq. To override it pass in masks. Note: this is not called during
    generation
    """
    pad_token_id = config.pad_token_id
    if decoder_input_ids is None:
        decoder_input_ids = shift_tokens_right(input_ids, pad_token_id)
    bsz, tgt_len = decoder_input_ids.size()
    if decoder_padding_mask is None:
        decoder_padding_mask = make_padding_mask(decoder_input_ids, pad_token_id)
    else:
        decoder_padding_mask = invert_mask(decoder_padding_mask)
    causal_mask = triu_onnx(fill_with_neg_inf(torch.zeros(tgt_len, tgt_len, dtype=causal_mask_dtype)), 1).to(
        device=decoder_input_ids.device
    )
    return decoder_input_ids, decoder_padding_mask, causal_mask


@auto_docstring
class PretrainedFSMTModel(PreTrainedModel):
    config: FSMTConfig
    base_model_prefix = "model"

    def _init_weights(self, module):
        std = self.config.init_std
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, SinusoidalPositionalEmbedding):
            weight = module.get_embedding(*module.weight.shape, module.padding_idx)
            weight = nn.Parameter(weight, requires_grad=False)
            weight.detach_()
            module.weight = weight
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    @property
    def dummy_inputs(self):
        pad_token = self.config.pad_token_id
        input_ids = torch.tensor([[0, 6, 10, 4, 2], [0, 8, 12, 2, pad_token]], device=self.device)
        dummy_inputs = {
            "attention_mask": input_ids.ne(pad_token),
            "input_ids": input_ids,
        }
        return dummy_inputs


def _make_linear_from_emb(emb):
    vocab_size, emb_size = emb.weight.shape
    lin_layer = nn.Linear(vocab_size, emb_size, bias=False)
    lin_layer.weight.data = emb.weight.data
    return lin_layer


# Helper Functions, mostly for making masks
def _check_shapes(shape_1, shape2):
    if shape_1 != shape2:
        raise AssertionError(f"shape mismatch: {shape_1} != {shape2}")


def shift_tokens_right(input_ids, pad_token_id):
    """Shift input ids one token to the right, and wrap the last non pad token (usually <eos>)."""

    # replace possible -100 values in labels by `pad_token_id`
    input_ids.masked_fill_(input_ids == -100, pad_token_id)

    prev_output_tokens = input_ids.clone()
    index_of_eos = (input_ids.ne(pad_token_id).sum(dim=1) - 1).unsqueeze(-1)
    prev_output_tokens[:, 0] = input_ids.gather(1, index_of_eos).squeeze()
    prev_output_tokens[:, 1:] = input_ids[:, :-1]
    return prev_output_tokens


def make_padding_mask(input_ids, padding_idx=1):
    """True for pad tokens"""
    padding_mask = input_ids.eq(padding_idx)
    if not padding_mask.any():
        padding_mask = None
    return padding_mask


# Helper Modules


class EncoderLayer(nn.Module):
    def __init__(self, config: FSMTConfig):
        super().__init__()
        self.embed_dim = config.d_model
        self.self_attn = Attention(self.embed_dim, config.encoder_attention_heads, dropout=config.attention_dropout)
        self.self_attn_layer_norm = LayerNorm(self.embed_dim)
        self.dropout = config.dropout
        self.activation_fn = ACT2FN[config.activation_function]
        self.activation_dropout = config.activation_dropout
        self.fc1 = nn.Linear(self.embed_dim, config.encoder_ffn_dim)
        self.fc2 = nn.Linear(config.encoder_ffn_dim, self.embed_dim)
        self.final_layer_norm = LayerNorm(self.embed_dim)

    def forward(self, x, encoder_padding_mask, layer_head_mask, output_attentions=False):
        """
        Args:
            x (`torch.Tensor`): input to the layer of shape *(seq_len, batch, embed_dim)*
            encoder_padding_mask (`torch.ByteTensor`): binary ByteTensor of shape
                *(batch, src_len)* where padding elements are indicated by `1`.
            for t_tgt, t_src is excluded (or masked out), =0 means it is
            included in attention
            layer_head_mask (`torch.FloatTensor`): mask for attention heads in a given layer of size
                *(config.encoder_attention_heads,)*.

        Returns:
            encoded output of shape *(seq_len, batch, embed_dim)*
        """
        residual = x
        x, attn_weights = self.self_attn(
            query=x,
            key=x,
            key_padding_mask=encoder_padding_mask,
            layer_head_mask=layer_head_mask,
            output_attentions=output_attentions,
        )
        x = nn.functional.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.self_attn_layer_norm(x)

        residual = x
        x = self.activation_fn(self.fc1(x))
        x = nn.functional.dropout(x, p=self.activation_dropout, training=self.training)
        x = self.fc2(x)
        x = nn.functional.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.final_layer_norm(x)
        return x, attn_weights


class FSMTEncoder(nn.Module):
    """
    Transformer encoder consisting of *config.encoder_layers* self attention layers. Each layer is a [`EncoderLayer`].

    Args:
        config: FSMTConfig
    """

    def __init__(self, config: FSMTConfig, embed_tokens):
        super().__init__()
        self.dropout = config.dropout
        self.layerdrop = config.encoder_layerdrop
        self.padding_idx = embed_tokens.padding_idx
        self.embed_tokens = embed_tokens
        embed_dim = embed_tokens.embedding_dim
        self.embed_scale = math.sqrt(embed_dim) if config.scale_embedding else 1.0
        self.embed_positions = SinusoidalPositionalEmbedding(
            config.max_position_embeddings + self.padding_idx + 1, embed_dim, self.padding_idx
        )
        self.layers = nn.ModuleList([EncoderLayer(config) for _ in range(config.encoder_layers)])  # type: list[EncoderLayer]

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
    ):
        """
        Args:
            input_ids (`torch.LongTensor`): tokens in the source language of shape
                *(batch, src_len)*
            attention_mask (`torch.LongTensor`): indicating which indices are padding tokens
            inputs_embeds (`torch.FloatTensor`):
                embedding vectors of shape *(batch, src_len, embed_dim)*
            head_mask (`torch.Tensor` of shape `(num_layers, num_heads)`, *optional*):
                Mask to nullify selected heads of the attention modules. Mask values selected in `[0, 1]`:

                - 1 indicates the head is **not masked**,
                - 0 indicates the head is **masked**.

        Returns:
            BaseModelOutput or Tuple comprised of:

                - **x** (`torch.Tensor`): the last encoder layer's output of shape *(src_len, batch, embed_dim)*
                - **encoder_states** (`Tuple(torch.FloatTensor)`): all intermediate hidden states of shape *(src_len,
                  batch, embed_dim)*. Only populated if *output_hidden_states:* is True.
                - **all_attentions** (`Tuple(torch.FloatTensor)`): Attention weights for each layer.
                During training might not be of length n_layers because of layer dropout.
        """
        # check attention mask and invert
        if attention_mask is not None:
            attention_mask = invert_mask(attention_mask)

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            inputs_embeds = self.embed_tokens(input_ids) * self.embed_scale
            embed_pos = self.embed_positions(input_ids)
        elif inputs_embeds is not None:
            inputs_embeds = inputs_embeds * self.embed_scale

            # We assume zeros hidden states correspond to padding tokens
            # and create `position_ids` where inputs_embeds[:, :, 0] == 0
            position_ids = inputs_embeds[:, :, 0].masked_fill(
                inputs_embeds[:, :, 0].eq(0), self.embed_positions.padding_idx
            )

            embed_pos = self.embed_positions(position_ids)
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        x = inputs_embeds + embed_pos
        x = nn.functional.dropout(x, p=self.dropout, training=self.training)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        encoder_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None
        # check if head_mask has a correct number of layers specified if desired
        if head_mask is not None:
            assert head_mask.size()[0] == (len(self.layers)), (
                f"The head_mask should be specified for {len(self.layers)} layers, but it is for {head_mask.size()[0]}."
            )
        for idx, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                x = x.transpose(0, 1)  # T x B x C -> B x T x C
                encoder_states += (x,)
                x = x.transpose(0, 1)  # B x T x C -> T x B x C
            # add LayerDrop (see https://huggingface.co/papers/1909.11556 for description)
            dropout_probability = torch.rand([])
            if self.training and (dropout_probability < self.layerdrop):  # skip the layer
                attn = None
            else:
                x, attn = encoder_layer(
                    x,
                    attention_mask,
                    layer_head_mask=(head_mask[idx] if head_mask is not None else None),
                    output_attentions=output_attentions,
                )

            if output_attentions:
                all_attentions = all_attentions + (attn,)

        # T x B x C -> B x T x C
        x = x.transpose(0, 1)

        if output_hidden_states:
            encoder_states += (x,)

        if not return_dict:
            return tuple(v for v in [x, encoder_states, all_attentions] if v is not None)
        return BaseModelOutput(last_hidden_state=x, hidden_states=encoder_states, attentions=all_attentions)


class DecoderLayer(nn.Module):
    def __init__(self, config: FSMTConfig, layer_idx=None):
        super().__init__()
        self.embed_dim = config.d_model

        self.self_attn = Attention(
            embed_dim=self.embed_dim,
            num_heads=config.decoder_attention_heads,
            dropout=config.attention_dropout,
            layer_idx=layer_idx,
        )
        self.dropout = config.dropout
        self.activation_fn = ACT2FN[config.activation_function]
        self.activation_dropout = config.activation_dropout

        self.self_attn_layer_norm = LayerNorm(self.embed_dim)
        self.encoder_attn = Attention(
            self.embed_dim,
            config.decoder_attention_heads,
            dropout=config.attention_dropout,
            encoder_decoder_attention=True,
            layer_idx=layer_idx,
        )
        self.encoder_attn_layer_norm = LayerNorm(self.embed_dim)
        self.fc1 = nn.Linear(self.embed_dim, config.decoder_ffn_dim)
        self.fc2 = nn.Linear(config.decoder_ffn_dim, self.embed_dim)
        self.final_layer_norm = LayerNorm(self.embed_dim)

    def forward(
        self,
        x,
        encoder_hidden_states,
        encoder_attn_mask=None,
        layer_state=None,
        causal_mask=None,
        layer_head_mask=None,
        cross_attn_layer_head_mask=None,
        decoder_padding_mask=None,
        output_attentions=False,
        cache_position=None,
    ):
        residual = x

        # Self Attention
        x, self_attn_weights = self.self_attn(
            query=x,
            key=x,
            layer_state=layer_state,  # adds keys to layer state
            key_padding_mask=decoder_padding_mask,
            attn_mask=causal_mask,
            layer_head_mask=layer_head_mask,
            output_attentions=output_attentions,
            cache_position=cache_position,
        )
        x = nn.functional.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.self_attn_layer_norm(x)

        # Cross attention
        residual = x
        assert self.encoder_attn.cache_key != self.self_attn.cache_key
        x, cross_attn_weights = self.encoder_attn(
            query=x,
            key=encoder_hidden_states,
            key_padding_mask=encoder_attn_mask,
            layer_state=layer_state,  # mutates layer state
            layer_head_mask=cross_attn_layer_head_mask,
            output_attentions=output_attentions,
            cache_position=cache_position,
        )
        x = nn.functional.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.encoder_attn_layer_norm(x)

        # Fully Connected
        residual = x
        x = self.activation_fn(self.fc1(x))
        x = nn.functional.dropout(x, p=self.activation_dropout, training=self.training)
        x = self.fc2(x)
        x = nn.functional.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.final_layer_norm(x)
        return (
            x,
            self_attn_weights,
            cross_attn_weights,
        )


class FSMTDecoder(nn.Module):
    """
    Transformer decoder consisting of *config.decoder_layers* layers. Each layer is a [`DecoderLayer`]

    Args:
        config: FSMTConfig
        embed_tokens (nn.Embedding): output embedding
    """

    def __init__(self, config: FSMTConfig, embed_tokens: nn.Embedding):
        super().__init__()
        self.dropout = config.dropout
        self.layerdrop = config.decoder_layerdrop
        self.padding_idx = embed_tokens.padding_idx
        self.embed_scale = math.sqrt(config.d_model) if config.scale_embedding else 1.0
        self.embed_tokens = embed_tokens
        embed_dim = embed_tokens.embedding_dim
        self.embed_positions = SinusoidalPositionalEmbedding(
            config.max_position_embeddings + self.padding_idx + 1, embed_dim, self.padding_idx
        )
        self.layers = nn.ModuleList([DecoderLayer(config, layer_idx=i) for i in range(config.decoder_layers)])  # type: list[DecoderLayer]

        if is_deepspeed_zero3_enabled():
            import deepspeed

            with deepspeed.zero.GatheredParameters(self.embed_tokens.weight, modifier_rank=None):
                embed_tokens_weight_shape = self.embed_tokens.weight.shape
        else:
            embed_tokens_weight_shape = self.embed_tokens.weight.shape
        self.output_projection = nn.Linear(embed_tokens_weight_shape[1], embed_tokens_weight_shape[0], bias=False)
        self.output_projection.weight = self.embed_tokens.weight

    def _tie_weights(self):
        self.embed_tokens.weight = self.output_projection.weight

    def forward(
        self,
        input_ids: torch.Tensor,
        encoder_hidden_states: torch.Tensor,
        encoder_padding_mask: torch.Tensor,
        decoder_padding_mask: torch.Tensor,
        decoder_causal_mask: torch.Tensor,
        head_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        cross_attn_head_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[list[torch.FloatTensor]] = None,
        use_cache: Optional[bool] = False,
        output_attentions: Optional[bool] = False,
        output_hidden_states: Optional[bool] = False,
        return_dict: Optional[bool] = True,
        cache_position: Optional[torch.Tensor] = None,
    ):
        """
        Includes several features from "Jointly Learning to Align and Translate with Transformer Models" (Garg et al.,
        EMNLP 2019).

        Args:
            input_ids (`torch.LongTensor` of shape `(batch, tgt_len)`):
                previous decoder outputs for teacher forcing
            encoder_hidden_states: output from the encoder, used for
                encoder-side attention
            encoder_padding_mask: for ignoring pad tokens
            past_key_values (dict or None): dictionary used for storing state during generation
            head_mask (`torch.Tensor` of shape `(num_layers, num_heads)`, *optional*):
                Mask to nullify selected heads of the attention modules. Mask values selected in `[0, 1]`:

                - 1 indicates the head is **not masked**,
                - 0 indicates the head is **masked**.

            cross_attn_head_mask (`torch.Tensor` of shape `(num_layers, num_heads)`, *optional*):
                Mask to nullify selected heads of the cross-attention modules. Mask values selected in `[0, 1]`:

                - 1 indicates the head is **not masked**,
                - 0 indicates the head is **masked**.

        Returns:
            BaseModelOutputWithPast or tuple:

                - the decoder's features of shape *(batch, tgt_len, embed_dim)*
                - the cache
                - hidden states
                - attentions
        """
        # check attention mask and invert
        if encoder_padding_mask is not None:
            encoder_padding_mask = invert_mask(encoder_padding_mask)

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both decoder_input_ids and decoder_inputs_embeds at the same time")
        elif input_ids is not None:
            # embed positions
            positions = self.embed_positions(input_ids)
            if use_cache:
                input_ids = input_ids[:, -1:]
                positions = positions[:, -1:]  # happens after we embed them
            x = self.embed_tokens(input_ids) * self.embed_scale
        elif inputs_embeds is not None:
            # We assume zeros hidden states correspond to padding tokens
            # and create `position_ids` where inputs_embeds[:, :, 0] == 0
            position_ids = inputs_embeds[:, :, 0].masked_fill(
                inputs_embeds[:, :, 0].eq(0), self.embed_positions.padding_idx
            )
            positions = self.embed_positions(position_ids)
            x = inputs_embeds * self.embed_scale
        else:
            raise ValueError("You have to specify either decoder_input_ids or decoder_inputs_embeds")

        # initialize `past_key_values`
        return_legacy_cache = False
        if use_cache and not isinstance(past_key_values, Cache):
            return_legacy_cache = True
            logger.warning_once(
                "Passing a tuple of `past_key_values` is deprecated and will be removed in Transformers v4.58.0. "
                "You should pass an instance of `EncoderDecoderCache` instead, e.g. "
                "`past_key_values=EncoderDecoderCache.from_legacy_cache(past_key_values)`."
            )
            past_key_values = EncoderDecoderCache.from_legacy_cache(past_key_values)

        x += positions
        x = nn.functional.dropout(x, p=self.dropout, training=self.training)

        # Convert to FSMT output format: (BS, seq_len, model_dim) -> (seq_len, BS, model_dim)
        x = x.transpose(0, 1)
        encoder_hidden_states = encoder_hidden_states.transpose(0, 1)

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        all_cross_attns = () if output_attentions else None

        # check if head_mask has a correct number of layers specified if desired
        for attn_mask, mask_name in zip([head_mask, cross_attn_head_mask], ["head_mask", "cross_attn_head_mask"]):
            if attn_mask is not None:
                assert attn_mask.size()[0] == (len(self.layers)), (
                    f"The `{mask_name}` should be specified for {len(self.layers)} layers, but it is for"
                    f" {head_mask.size()[0]}."
                )
        for idx, decoder_layer in enumerate(self.layers):
            # add LayerDrop (see https://huggingface.co/papers/1909.11556 for description)
            if output_hidden_states:
                x = x.transpose(0, 1)
                all_hidden_states += (x,)
                x = x.transpose(0, 1)
            if self.training:
                dropout_probability = torch.rand([])
                if dropout_probability < self.layerdrop:
                    continue

            x, layer_self_attn, layer_cross_attn = decoder_layer(
                x,
                encoder_hidden_states,
                encoder_attn_mask=encoder_padding_mask,
                decoder_padding_mask=decoder_padding_mask,
                layer_state=past_key_values,
                causal_mask=decoder_causal_mask,
                layer_head_mask=(head_mask[idx] if head_mask is not None else None),
                cross_attn_layer_head_mask=(cross_attn_head_mask[idx] if cross_attn_head_mask is not None else None),
                output_attentions=output_attentions,
                cache_position=cache_position,
            )

            if output_attentions:
                all_self_attns += (layer_self_attn,)
                all_cross_attns += (layer_cross_attn,)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            x = x.transpose(0, 1)
            all_hidden_states += (x,)
            x = x.transpose(0, 1)

        # Convert to standard output format: (seq_len, BS, model_dim) -> (BS, seq_len, model_dim)
        x = x.transpose(0, 1)
        encoder_hidden_states = encoder_hidden_states.transpose(0, 1)

        x = self.output_projection(x)

        if return_legacy_cache:
            past_key_values = past_key_values.to_legacy_cache()

        if not return_dict:
            return tuple(
                v for v in [x, past_key_values, all_hidden_states, all_self_attns, all_cross_attns] if v is not None
            )
        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=x,
            past_key_values=past_key_values,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
            cross_attentions=all_cross_attns,
        )


def _reorder_buffer(attn_cache, new_order):
    for k, input_buffer_k in attn_cache.items():
        if input_buffer_k is not None:
            attn_cache[k] = input_buffer_k.index_select(0, new_order)
    return attn_cache


class Attention(nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""

    def __init__(
        self,
        embed_dim,
        num_heads,
        dropout=0.0,
        bias=True,
        encoder_decoder_attention=False,  # otherwise self_attention
        layer_idx=None,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"
        self.scaling = self.head_dim**-0.5
        self.layer_idx = layer_idx

        self.encoder_decoder_attention = encoder_decoder_attention
        self.k_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.v_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.q_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=bias)
        self.cache_key = "encoder_decoder" if self.encoder_decoder_attention else "self"

    def forward(
        self,
        query,
        key: Optional[Tensor],
        key_padding_mask: Optional[Tensor] = None,
        layer_state: Optional[Cache] = None,
        attn_mask: Optional[Tensor] = None,
        layer_head_mask: Optional[Tensor] = None,
        output_attentions: Optional[bool] = False,
        cache_position: Optional[torch.Tensor] = None,
    ) -> tuple[Tensor, Optional[Tensor]]:
        """Input shape: Time(SeqLen) x Batch x Channel"""
        tgt_len, bsz, embed_dim = query.size()
        assert embed_dim == self.embed_dim
        assert list(query.size()) == [tgt_len, bsz, embed_dim]

        if layer_state is not None:
            if isinstance(layer_state, EncoderDecoderCache):
                is_updated = layer_state.is_updated.get(self.layer_idx)
                if self.encoder_decoder_attention:
                    # after the first generated id, we can subsequently re-use all key/value_states from cache
                    curr_past_key_value = layer_state.cross_attention_cache
                else:
                    curr_past_key_value = layer_state.self_attention_cache
            else:
                curr_past_key_value = layer_state

        # NOTE: FSMT has format (seq_len, BS, model_dim) ofr inputs
        current_states = key if self.encoder_decoder_attention else query
        if self.encoder_decoder_attention and layer_state is not None and is_updated:
            # reuse k,v, cross_attentions
            key_states = curr_past_key_value.layers[self.layer_idx].keys
            value_states = curr_past_key_value.layers[self.layer_idx].values
        else:
            key_states = self.k_proj(current_states)
            value_states = self.v_proj(current_states)
            key_states = key_states.view(-1, bsz, self.num_heads, self.head_dim).permute(1, 2, 0, 3)
            value_states = value_states.view(-1, bsz, self.num_heads, self.head_dim).permute(1, 2, 0, 3)

            if layer_state is not None:
                # save all key/value_states to cache to be re-used for fast auto-regressive generation
                cache_position = cache_position if not self.encoder_decoder_attention else None
                key_states, value_states = curr_past_key_value.update(
                    key_states, value_states, self.layer_idx, {"cache_position": cache_position}
                )
                # set flag that curr layer for cross-attn is already updated so we can re-use in subsequent calls
                if self.encoder_decoder_attention:
                    layer_state.is_updated[self.layer_idx] = True

        query_states = self.q_proj(query) * self.scaling

        # Reshape back to 3D tensors for `bmm`
        query_states = query_states.view(-1, bsz * self.num_heads, self.head_dim).transpose(0, 1)
        key_states = key_states.reshape(bsz * self.num_heads, -1, self.head_dim)
        value_states = value_states.reshape(bsz * self.num_heads, -1, self.head_dim)

        assert key_states is not None
        src_len = key_states.size(1)
        attn_weights = torch.bmm(query_states, key_states.transpose(1, 2))
        assert attn_weights.size() == (bsz * self.num_heads, tgt_len, src_len)

        if attn_mask is not None:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len) + attn_mask
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        # This is part of a workaround to get around fork/join parallelism not supporting Optional types.
        if key_padding_mask is not None and key_padding_mask.dim() == 0:
            key_padding_mask = None
        assert key_padding_mask is None or key_padding_mask.size()[:2] == (
            bsz,
            src_len,
        )

        if key_padding_mask is not None:  # don't attend to padding symbols
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            reshaped = key_padding_mask.unsqueeze(1).unsqueeze(2)
            attn_weights = attn_weights.masked_fill(reshaped, torch.finfo(attn_weights.dtype).min)
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        attn_weights = nn.functional.softmax(attn_weights, dim=-1)

        if layer_head_mask is not None:
            assert layer_head_mask.size() == (self.num_heads,), (
                f"Head mask for a single layer should be of size {(self.num_heads,)}, but is {layer_head_mask.size()}"
            )
            attn_weights = layer_head_mask.view(1, -1, 1, 1) * attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        if output_attentions:
            # make sure that attn_weights are included in graph
            attn_weights_reshaped = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights_reshaped.view(bsz * self.num_heads, tgt_len, src_len)
        else:
            attn_weights_reshaped = None

        attn_probs = nn.functional.dropout(
            attn_weights,
            p=self.dropout,
            training=self.training,
        )

        assert value_states is not None
        attn_output = torch.bmm(attn_probs, value_states)
        assert attn_output.size() == (bsz * self.num_heads, tgt_len, self.head_dim)
        attn_output = attn_output.transpose(0, 1).contiguous().view(tgt_len, bsz, embed_dim)
        attn_output = self.out_proj(attn_output)

        return attn_output, attn_weights_reshaped


def fill_with_neg_inf(t):
    """FP16-compatible function that fills a input_ids with -inf."""
    return t.float().fill_(torch.finfo(t.dtype).min).type_as(t)


# Public API
def _get_shape(t):
    return getattr(t, "shape", None)


@auto_docstring
class FSMTModel(PretrainedFSMTModel):
    _tied_weights_keys = ["decoder.embed_tokens.weight", "decoder.output_projection.weight"]

    def __init__(self, config: FSMTConfig):
        super().__init__(config)

        padding_idx = config.pad_token_id
        encoder_embed_tokens = nn.Embedding(config.src_vocab_size, config.d_model, padding_idx)
        decoder_embed_tokens = nn.Embedding(config.tgt_vocab_size, config.d_model, padding_idx)

        self.encoder = FSMTEncoder(config, encoder_embed_tokens)
        self.decoder = FSMTDecoder(config, decoder_embed_tokens)

        # Initialize weights and apply final processing
        self.post_init()

    def get_encoder(self):
        return self.encoder

    def get_decoder(self):
        return self.decoder

    def _tie_weights(self):
        if self.config.tie_word_embeddings:
            self._tie_or_clone_weights(self.decoder.embed_tokens, self.get_input_embeddings())
            self._tie_or_clone_weights(self.decoder.output_projection, self.get_input_embeddings())

    @auto_docstring
    def forward(
        self,
        input_ids: torch.LongTensor,
        attention_mask: Optional[torch.Tensor] = None,
        decoder_input_ids: Optional[torch.LongTensor] = None,
        decoder_attention_mask: Optional[torch.BoolTensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        decoder_head_mask: Optional[torch.Tensor] = None,
        cross_attn_head_mask: Optional[torch.Tensor] = None,
        encoder_outputs: Optional[tuple[torch.FloatTensor]] = None,
        past_key_values: Optional[tuple[torch.FloatTensor]] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        decoder_inputs_embeds: Optional[torch.FloatTensor] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.Tensor] = None,
    ) -> Union[tuple[torch.Tensor], Seq2SeqModelOutput]:
        r"""
        decoder_input_ids (`torch.LongTensor` of shape `(batch_size, target_sequence_length)`, *optional*):
            Indices of decoder input sequence tokens in the vocabulary.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are decoder input IDs?](../glossary#decoder-input-ids)

            FSMT uses the `eos_token_id` as the starting token for `decoder_input_ids` generation. If `past_key_values`
            is used, optionally only the last `decoder_input_ids` have to be input (see `past_key_values`).
        decoder_attention_mask (`torch.BoolTensor` of shape `(batch_size, target_sequence_length)`, *optional*):
            Default behavior: generate a tensor that ignores pad tokens in `decoder_input_ids`. Causal mask will also
            be used by default.
        cross_attn_head_mask (`torch.Tensor` of shape `(decoder_layers, decoder_attention_heads)`, *optional*):
            Mask to nullify selected heads of the cross-attention modules in the decoder. Mask values selected in `[0,
            1]`:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.
        """
        if decoder_input_ids is None:
            use_cache = False

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # make masks if user doesn't supply
        if not use_cache and input_ids is not None:
            decoder_input_ids, decoder_padding_mask, causal_mask = _prepare_fsmt_decoder_inputs(
                self.config,
                input_ids,
                decoder_input_ids=decoder_input_ids,
                decoder_padding_mask=decoder_attention_mask,
                causal_mask_dtype=self.decoder.embed_tokens.weight.dtype,
            )
        else:
            decoder_padding_mask, causal_mask = None, None

        if decoder_input_ids is None and decoder_inputs_embeds is None:
            raise ValueError("Make sure that `decoder_input_ids` or `decoder_inputs_embeds` are passed.")

        if encoder_outputs is None:
            encoder_outputs = self.encoder(
                input_ids=input_ids,
                attention_mask=attention_mask,
                inputs_embeds=inputs_embeds,
                head_mask=head_mask,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
        # If the user passed a tuple for encoder_outputs, we wrap it in a BaseModelOutput when return_dict=False
        elif return_dict and not isinstance(encoder_outputs, BaseModelOutput):
            encoder_outputs = BaseModelOutput(
                last_hidden_state=encoder_outputs[0],
                hidden_states=encoder_outputs[1] if len(encoder_outputs) > 1 else None,
                attentions=encoder_outputs[2] if len(encoder_outputs) > 2 else None,
            )

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        decoder_outputs = self.decoder(
            decoder_input_ids,
            encoder_outputs[0],
            attention_mask,
            decoder_padding_mask,
            decoder_causal_mask=causal_mask,
            inputs_embeds=decoder_inputs_embeds,
            head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            cache_position=cache_position,
        )

        if not return_dict:
            return decoder_outputs + encoder_outputs

        return Seq2SeqModelOutput(
            last_hidden_state=decoder_outputs.last_hidden_state,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )

    def get_input_embeddings(self):
        return self.encoder.embed_tokens

    def set_input_embeddings(self, value):
        self.encoder.embed_tokens = value

    def get_output_embeddings(self):
        return self.decoder.embed_tokens

    def set_output_embeddings(self, value):
        self.decoder.embed_tokens = value


@auto_docstring(
    custom_intro="""
    The FSMT Model with a language modeling head. Can be used for summarization.
    """
)
class FSMTForConditionalGeneration(PretrainedFSMTModel, GenerationMixin):
    base_model_prefix = "model"
    _tied_weights_keys = ["decoder.embed_tokens.weight", "decoder.output_projection.weight"]

    def __init__(self, config: FSMTConfig):
        super().__init__(config)
        base_model = FSMTModel(config)
        self.model = base_model

        # Initialize weights and apply final processing
        self.post_init()

    @auto_docstring
    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        decoder_input_ids: Optional[torch.LongTensor] = None,
        decoder_attention_mask: Optional[torch.BoolTensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        decoder_head_mask: Optional[torch.Tensor] = None,
        cross_attn_head_mask: Optional[torch.Tensor] = None,
        encoder_outputs: Optional[tuple[torch.FloatTensor]] = None,
        past_key_values: Optional[tuple[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        decoder_inputs_embeds: Optional[torch.Tensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.Tensor] = None,
    ) -> Union[tuple[torch.Tensor], Seq2SeqLMOutput]:
        r"""
        decoder_input_ids (`torch.LongTensor` of shape `(batch_size, target_sequence_length)`, *optional*):
            Indices of decoder input sequence tokens in the vocabulary.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are decoder input IDs?](../glossary#decoder-input-ids)

            FSMT uses the `eos_token_id` as the starting token for `decoder_input_ids` generation. If `past_key_values`
            is used, optionally only the last `decoder_input_ids` have to be input (see `past_key_values`).
        decoder_attention_mask (`torch.BoolTensor` of shape `(batch_size, target_sequence_length)`, *optional*):
            Default behavior: generate a tensor that ignores pad tokens in `decoder_input_ids`. Causal mask will also
            be used by default.
        cross_attn_head_mask (`torch.Tensor` of shape `(decoder_layers, decoder_attention_heads)`, *optional*):
            Mask to nullify selected heads of the cross-attention modules in the decoder. Mask values selected in `[0,
            1]`:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.
        labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
            config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
            (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Example Translation:

        ```python
        >>> from transformers import AutoTokenizer, FSMTForConditionalGeneration

        >>> mname = "facebook/wmt19-ru-en"
        >>> model = FSMTForConditionalGeneration.from_pretrained(mname)
        >>> tokenizer = AutoTokenizer.from_pretrained(mname)

        >>> src_text = "Машинное обучение - это здорово, не так ли?"
        >>> input_ids = tokenizer(src_text, return_tensors="pt").input_ids
        >>> outputs = model.generate(input_ids, num_beams=5, num_return_sequences=3)
        >>> tokenizer.decode(outputs[0], skip_special_tokens=True)
        "Machine learning is great, isn't it?"
        ```
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if labels is not None:
            use_cache = False

        outputs = self.model(
            input_ids,
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_inputs_embeds=decoder_inputs_embeds,
            encoder_outputs=encoder_outputs,
            decoder_attention_mask=decoder_attention_mask,
            head_mask=head_mask,
            decoder_head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            cache_position=cache_position,
        )
        lm_logits = outputs[0]

        masked_lm_loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # TODO(SS): do we need to ignore pad tokens in labels?
            masked_lm_loss = loss_fct(lm_logits.view(-1, self.config.tgt_vocab_size), labels.view(-1))

        if not return_dict:
            output = (lm_logits,) + outputs[1:]
            return ((masked_lm_loss,) + output) if masked_lm_loss is not None else output

        return Seq2SeqLMOutput(
            loss=masked_lm_loss,
            logits=lm_logits,
            past_key_values=outputs.past_key_values,
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )

    def prepare_decoder_input_ids_from_labels(self, labels: torch.Tensor):
        return shift_tokens_right(labels, self.config.pad_token_id)

    def get_encoder(self):
        return self.model.encoder

    def get_decoder(self):
        return self.model.decoder

    def get_output_embeddings(self):
        return self.model.decoder.embed_tokens

    def set_output_embeddings(self, value):
        self.model.decoder.embed_tokens = value


class SinusoidalPositionalEmbedding(nn.Embedding):
    """
    This module produces sinusoidal positional embeddings of any length.

    We don't want to save the weight of this embedding since it's not trained (deterministic) and it can be huge.

    Padding symbols are ignored.

    These embeddings get automatically extended in forward if more positions is needed.
    """

    def __init__(self, num_positions, embedding_dim, padding_idx):
        super().__init__(num_positions, embedding_dim, padding_idx)

    def make_weight(self, num_positions, embedding_dim, padding_idx):
        weight = self.get_embedding(num_positions, embedding_dim, padding_idx)
        # in forward put the weights on the correct dtype and device of the param
        weight = weight.to(dtype=self.weight.dtype, device=self.weight.device)
        self.weight = nn.Parameter(weight)
        self.weight.detach_()
        self.weight.requires_grad = False

    @staticmethod
    def get_embedding(num_embeddings, embedding_dim, padding_idx):
        """
        Build sinusoidal embeddings.

        This matches the implementation in tensor2tensor, but differs slightly from the description in Section 3.5 of
        "Attention Is All You Need".
        """
        half_dim = embedding_dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, dtype=torch.int64).float() * -emb)
        emb = torch.arange(num_embeddings, dtype=torch.int64).float().unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat([torch.sin(emb), torch.cos(emb)], dim=1).view(num_embeddings, -1)
        if embedding_dim % 2 == 1:
            # zero pad
            emb = torch.cat([emb, torch.zeros(num_embeddings, 1)], dim=1)
        if padding_idx is not None:
            emb[padding_idx, :] = 0
        return emb

    @staticmethod
    def make_positions(tensor, padding_idx: int):
        """
        Replace non-padding symbols with their position numbers.

        Position numbers begin at padding_idx+1. Padding symbols are ignored.
        """
        # The series of casts and type-conversions here are carefully
        # balanced to both work with ONNX export and XLA. In particular XLA
        # prefers ints, cumsum defaults to output longs, and ONNX doesn't know
        # how to handle the dtype kwarg in cumsum.
        mask = tensor.ne(padding_idx).int()
        return (torch.cumsum(mask, dim=1).type_as(mask) * mask).long() + padding_idx

    def forward(
        self,
        input,
        incremental_state: Optional[Any] = None,
        timestep: Optional[Tensor] = None,
    ):
        """Input is expected to be of size [bsz x seqlen]."""
        bsz, seq_len = input.shape[:2]
        max_pos = self.padding_idx + 1 + seq_len
        if max_pos > self.weight.size(0):
            # expand embeddings if needed
            self.make_weight(max_pos, self.embedding_dim, self.padding_idx)
        positions = self.make_positions(input, self.padding_idx)
        return super().forward(positions)


__all__ = ["FSMTForConditionalGeneration", "FSMTModel", "PretrainedFSMTModel"]
