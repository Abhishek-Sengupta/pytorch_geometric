from typing import Dict, List, Optional, Tuple, Union

import torch
from torch_sparse import SparseTensor

from .inspector import Inspector
from .collector import Collector


class MessagePassing(torch.nn.Module):

    AdjType = Union[torch.Tensor, SparseTensor]
    adj_formats: List[str] = ['edge_index', 'sparse', 'dense']
    mp_formats: List[str] = ['fused', 'sparse', 'dense']
    suffixes: List[str] = ['_i', '_j']

    __collectors__: Dict[str, Collector] = {
        ('edge_index', 'sparse'): Collector(),
    }

    def __init__(self, aggr: str = "add", flow: str = "source_to_target",
                 format: Optional[str] = None, node_dim: int = 0,
                 partial_max_deg: Optional[int] = None,
                 partial_binning: bool = True, torchscript: bool = False):
        super(MessagePassing, self).__init__()

        self.aggr: str = aggr
        self.flow: str = flow
        self.format: Optional[str] = format
        self.node_dim: int = node_dim
        self.partial_max_deg: Optional[int] = partial_max_deg
        self.partial_binning: bool = partial_binning
        self.torchscript: bool = torchscript

        assert self.aggr in ['add', 'sum', 'mean', 'max', None]
        assert self.flow in ['source_to_target', 'target_to_source']
        assert self.format in self.mp_formats + [None]
        assert self.node_dim >= 0

        self.inspector = Inspector(self)
        self.inspector.inspect(self.message_and_aggregate)
        self.inspector.inspect(self.message)
        self.inspector.inspect(self.aggregate, pop_first=True)
        self.inspector.inspect(self.partial_message)
        self.inspector.inspect(self.partial_aggregate, pop_first=True)
        self.inspector.inspect(self.update, pop_first=True)

        self.__cached_mp_format__ = {}

        # Support for `GNNExplainer`.
        self.__explain__: bool = False
        self.__edge_mask__: bool = None

    def supports_fused_format(self) -> bool:
        return self.inspector.implements('message_and_aggregate')

    def supports_sparse_format(self) -> bool:
        return (self.inspector.implements('message')
                and (self.inspector.implements('aggregate')
                     or self.aggr is not None))

    def supports_partial_format(self) -> bool:
        return (self.inspector.implements('partial_message')
                and (self.inspector.implements('partial_aggregate')
                     or self.aggr is not None))

    def get_adj_format(self, adj_type: AdjType) -> str:
        adj_format = None

        # edge_index: torch.LongTensor of shape [2, *].
        if (torch.is_tensor(adj_type) and adj_type.dim() == 2
                and adj_type.size(0) == 2 and adj_type.dtype == torch.long):
            adj_format = 'edge_index'

        # sparse_adj: torch_sparse.SparseTensor.
        elif isinstance(adj_type, SparseTensor):
            adj_format = 'sparse_adj'

        # dense_adj: *Any* torch.Tensor.
        elif torch.is_tensor(adj_type):
            adj_format = 'dense_adj'

        if adj_format is None:
            raise ValueError(
                ('Encountered an invalid object for `adj_type` in '
                 '`MessagePassing.propagate`. Supported types are (1) sparse '
                 'edge indices of type `torch.LongTensor` with shape '
                 '`[2, num_edges]`, (2) sparse adjacency matrices of type '
                 '`torch_sparse.SparseTensor`, or (3) dense adjacency '
                 'matrices of type `torch.Tensor`.'))

        return adj_format

    def get_mp_format(self, adj_format: str) -> str:
        mp_format = None

        # Use already determined cached message passing format (if present).
        if adj_format in self.__cached_mp_format__:
            mp_format = self.__cached_mp_format__[adj_format]

        # `edge_index` only support "tradional" message passing, i.e. "sparse".
        elif adj_format == 'edge_index':
            mp_format = 'sparse'

        # Set to user-desired format (if present).
        elif self.format is not None:
            mp_format = self.format

        # Always choose `fused` if applicable.
        elif self.supports_fused_format():
            mp_format = 'fused'

        # We prefer "sparse" format over the "partial" format for sparse
        # adjacency matrices since it is faster in general. We therefore only
        # default to "partial" mode if the user wants to implement some fancy
        # customized aggregation scheme.
        elif adj_format == 'sparse_adj' and self.supports_sparse_format():
            mp_format = 'sparse'
        elif adj_format == 'sparse_adj' and self.supports_partial_format():
            mp_format = 'partial'

        # For "dense" adjacencies, we *require* "partial" aggregation.
        elif adj_format == 'dense_adj' and self.supports_partial_format():
            mp_format = 'partial'

        if mp_format is None:
            raise TypeError(
                (f'Could not detect a valid message passing implementation '
                 f'for adjacency format "{adj_format}".'))

        # Fill (or update) the cache.
        self.__cached_mp_format__[adj_format] = mp_format

        return mp_format

    def __get_collector__(self, adj_format: str, mp_format: str) -> Collector:
        collector = self.__collectors__.get((adj_format, mp_format), None)

        if collector is None:
            raise TypeError(
                (f'Could not detect a valid message passing implementation '
                 f'for adjacency format "{adj_format}" and message passing '
                 f'format "{mp_format}".'))

        collector.bind(self)

        return collector

    def __suffix2id__(self) -> Dict[str, int]:
        suffix2idx: Dict[str, int] = {'_i': 1, '_j': 0}
        if self.flow == 'target_to_source':
            suffix2idx = {'_i': 0, '_j': 1}
        return suffix2idx

    def propagate(self, adj_type: AdjType, size: Optional[Tuple[int]] = None,
                  **kwargs) -> torch.Tensor:

        adj_format = self.get_adj_format(adj_type)
        mp_format = self.get_mp_format(adj_format)

        # For `GNNExplainer`, we require "sparse" aggregation since this allows
        # us to easily inject `edge_mask` into the message passing computation.
        # NOTE: Technically, it is still possible to provide this for all
        # types of message passing. However, for other formats it is a lot
        # hackier to implement, so we leave this for future work at the moment.
        if self.__explain__:
            if adj_format != 'edge_index' or not self.supports_sparse_format():
                raise TypeError(
                    ('`MessagePassing.propagate` only supports `GNNExplainer` '
                     'capabilties for "sparse" aggregations based on '
                     '"edge_index".`'))
            mp_format = 'sparse'

        # Customized flow direction is deprecated for "new" adjacency matrix
        # formats, i.e., "sparse_adj" and "dense_adj".
        if ((adj_format == 'sparse_adj' or adj_format == 'dense_adj')
                and self.flow == 'target_to_source'):
            raise TypeError(
                ('Flow direction "target_to_source" is invalid for message '
                 'passing based on adjacency matrices. If you really want to '
                 'make use of reverse message passing flow, pass in the '
                 'transposed adjacency matrix to the message passing module, '
                 'e.g., `adj_t.t()`.'))

        # We collect all arguments used for message passing dependening on the
        # determined collector.
        collector = self.__get_collector__(adj_format, mp_format)
        kwargs = collector.collect(adj_type, size, kwargs)

        # Perform conditional message passing.
        if mp_format == 'fused':
            inp = self.inspector.distribute(self.message_and_aggregate, kwargs)
            out = self.message_and_aggregate(**inp)

        elif mp_format == 'sparse':
            inp = self.inspector.distribute(self.message, kwargs)
            out = self.message(**inp)

            if self.__explain__:
                edge_mask = self.__edge_mask__.sigmoid()
                if out.size(0) != edge_mask.size(0):
                    # NOTE: This does only work for "edge_index" format.
                    # TODO: Make use of unified `add_self_loops` interface.
                    loop = edge_mask.new_ones(size[0])
                    edge_mask = torch.cat([edge_mask, loop], dim=0)
                assert out.size(0) == edge_mask.size(0)
                out = out * edge_mask.view(-1, 1)

            inp = self.inspector.distribute(self.aggregate, kwargs)
            out = self.aggregate(out, **inp)

        elif mp_format == 'partial':
            inp = self.inspector.distribute(self.partial_message, kwargs)
            out = self.__partial_message__(**inp)
            inp = self.inspector.distribute(self.partial_aggregate, kwargs)
            out = self.__partial__aggregate__(out, **inp)

        inp = self.inspector.distribute(self.update, kwargs)
        out = self.update(out, **inp)

        return out

    def message_and_aggregate(self) -> torch.Tensor:
        raise NotImplementedError

    def message(self) -> torch.Tensor:
        raise NotImplementedError

    def aggregate(self, inputs: torch.Tensor, index: torch.Tensor,
                  ptr: Optional[torch.Tensor] = None,
                  dim_size: Optional[int] = None) -> torch.Tensor:
        raise NotImplementedError

    def partial_message(self) -> torch.Tensor:
        raise NotImplementedError

    def partial_aggregate(self, inputs) -> torch.Tensor:
        raise NotImplementedError

    def update(self, inputs: torch.Tensor) -> torch.Tensor:
        return inputs

    def __partial__message__(self, kwargs):
        pass

    def __partial__aggregate__(self, kwargs):
        pass

    def check_propagate_consistency(self, adj_type: AdjType,
                                    size: Optional[Tuple[int]] = None,
                                    **kwargs) -> bool:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}()'