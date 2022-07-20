from typing import Any, Dict, List, Optional, Union

import torch
from torch import Tensor
from torch.nn import Linear, MultiheadAttention

from torch_geometric.nn.aggr import Aggregation
from torch_geometric.nn.resolver import aggregation_resolver


class MultiAggregation(Aggregation):
    r"""Performs aggregations with one or more aggregators and combines
        aggregated results.

    Args:
        aggrs (list): The list of aggregation schemes to use.
        aggrs_kwargs (list, optional): Arguments passed to the
            respective aggregation function in case it gets automatically
            resolved. (default: :obj:`None`)
        mode (string, optional): The combine mode to use for combining
            aggregated results from multiple aggregations (:obj:`"cat"`,
            :obj:`"proj"`, :obj:`"sum"`, :obj:`"mean"`, :obj:`"max"`,
            :obj:`"min"`, :obj:`"logsumexp"`, :obj:`"std"`, :obj:`"var"`,
            :obj:`"attn"`). (default: :obj:`"cat"`)
        in_channels (int or tuple, optional): Size of each input sample to
            combine from the repsective aggregation outputs. Need to be
            specified when :obj:`"proj"` is used as the combine `mode`.
            (default: :obj:`None`)
        out_channels (int, optional): Size of each output sample after
            combination. Need to be specified when :obj:`"proj"` is used as
            the combine `mode`. (default: :obj:`None`)
        num_heads (int, optional): Number of parallel attention heads for
            attention-based :obj:`"attn"` combine. (default: :obj:`None`)
        mode_kwargs (dict, optional): Additional arguments for combine `mode`.
            (default: :obj:`None`)
    """
    def __init__(
        self,
        aggrs: List[Union[Aggregation, str]],
        aggrs_kwargs: Optional[List[Dict[str, Any]]] = None,
        mode: Optional[str] = 'cat',
        in_channels: Optional[int] = None,
        out_channels: Optional[int] = None,
        num_heads: Optional[int] = None,
        mode_kwargs: Optional[Dict[str, Any]] = None,
    ):

        super().__init__()

        if not isinstance(aggrs, (list, tuple)):
            raise ValueError(f"'aggrs' of '{self.__class__.__name__}' should "
                             f"be a list or tuple (got '{type(aggrs)}').")

        if len(aggrs) == 0:
            raise ValueError(f"'aggrs' of '{self.__class__.__name__}' should "
                             f"not be empty.")

        if aggrs_kwargs is None:
            aggrs_kwargs = [{}] * len(aggrs)
        elif len(aggrs) != len(aggrs_kwargs):
            raise ValueError(f"'aggrs_kwargs' with invalid length passed to "
                             f"'{self.__class__.__name__}' "
                             f"(got '{len(aggrs_kwargs)}', "
                             f"expected '{len(aggrs)}'). Ensure that both "
                             f"'aggrs' and 'aggrs_kwargs' are consistent.")

        self.aggrs = torch.nn.ModuleList([
            aggregation_resolver(aggr, **aggr_kwargs)
            for aggr, aggr_kwargs in zip(aggrs, aggrs_kwargs)
        ])

        self.mode = mode
        mode_kwargs = mode_kwargs or {}
        if mode == 'proj' or mode == 'attn':
            if (in_channels and out_channels) is None:
                raise ValueError(
                    f"'Combine mode: '{mode}' must have `in_channels`"
                    f"and `out_channels` specified.")

            if isinstance(in_channels, int):
                in_channels = (in_channels, ) * len(aggrs)

            if mode == 'proj':
                self.lin = Linear(sum(in_channels), out_channels, bias=True)

            if mode == 'attn':
                if num_heads is None:
                    raise ValueError(
                        f"'Combine mode: '{mode}' must have `num_heads` "
                        f"specified.")
                self.need_weights = mode_kwargs.pop('need_weights', False)
                self.lin_heads = torch.nn.ModuleList([
                    Linear(channels, out_channels, bias=True)
                    for channels in in_channels
                ])
                self.multihead_attn = MultiheadAttention(
                    out_channels,
                    num_heads,
                    **mode_kwargs,
                )

        dense_combine_modes = [
            'sum', 'mean', 'max', 'min', 'logsumexp', 'std', 'var'
        ]
        if mode in dense_combine_modes:
            if (in_channels or out_channels or num_heads) is not None:
                raise ValueError("Channel transformation is only supported in "
                                 "the `'proj'` and `'attn'` combine mode.")
            self.dense_combine = getattr(torch, mode)

    def reset_parameters(self):
        for aggr in self.aggrs:
            aggr.reset_parameters()
        if self.mode == 'proj':
            self.lin.reset_parameters()
        if self.mode == 'att':
            for lin in self.lin_heads:
                lin.reset_parameters()
            self.multihead_attn.reset_parameters()

    def forward(self, x: Tensor, index: Optional[Tensor] = None,
                ptr: Optional[Tensor] = None, dim_size: Optional[int] = None,
                dim: int = -2) -> Tensor:
        outs = []
        for aggr in self.aggrs:
            outs.append(aggr(x, index, ptr, dim_size, dim))

        return self.combine(outs) if len(outs) > 1 else outs[0]

    def combine(self, inputs: List[Tensor]) -> Tensor:
        if self.mode in ['cat', 'proj']:
            out = torch.cat(inputs, dim=-1)
            return self.lin(out) if hasattr(self, 'lin') else out
        elif hasattr(self, 'multihead_attn'):
            self.lin_heads
            x = torch.stack(
                [head(x) for x, head in zip(inputs, self.lin_heads)],
                dim=0,
            )
            attn_out, attn_weights = self.multihead_attn(x, x, x)
            out = torch.mean(attn_out, dim=0)
            return (out, attn_weights) if self.need_weights else out
        elif hasattr(self, 'dense_combine'):
            out = self.dense_combine(torch.stack(inputs, dim=0), dim=0)
            return out if isinstance(out, Tensor) else out[0]
        else:
            raise ValueError(f"'Combine mode: '{self.mode}' is not "
                             f"supported")

    def __repr__(self) -> str:
        args = [f'  {aggr}' for aggr in self.aggrs]
        return '{}([\n{}\n], mode={})'.format(
            self.__class__.__name__,
            ',\n'.join(args),
            self.mode,
        )
