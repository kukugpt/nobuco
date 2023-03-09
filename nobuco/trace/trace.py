import inspect
import types
from copy import deepcopy
from typing import List, Collection, Callable, Union

import torch
import torchvision
from torch import nn

from nobuco.entity.pytorch import PytorchNode, WrappedOp, PytorchNodeHierarchy
from nobuco.util import collect_recursively, clone_torch_tensors_recursively


class Tracer:
    
    op_tracing_classes = [
        torch,
        torch.Tensor,
        torch.linalg,
        torch.nn.functional,
        torchvision.transforms.functional,
    ]

    op_blacklist = [
        torch.Tensor.__init__,
        torch.Tensor._make_subclass,
    ]

    op_whitelist_dict = {
        torch.Tensor: torch.Tensor.__setitem__
    }

    _tracing_enabled = False
    _parent_list = []
    _node_list = []

    @staticmethod
    def traceable():
        def inner(func_to_trace: Callable) -> Callable:
            if Tracer.is_decorated(func_to_trace):
                return func_to_trace
            else:
                module_suffix = func_to_trace.__qualname__
                module_suffix = '.'.join(module_suffix.split('.')[:-1])
                return Tracer.op_tracing_decorator(func_to_trace, inspect.getmodule(func_to_trace), module_suffix=module_suffix)
        return inner

    @staticmethod
    def is_decorated(callable) -> bool:
        return hasattr(callable, '__undecorated_func__')

    @staticmethod
    def op_unwrap(callable) -> bool:
        if Tracer.is_decorated(callable):
            return callable.__undecorated_func__
        else:
            return callable

    @staticmethod
    def are_equal(tensors1, tensors2):
        tensors1 = collect_recursively(tensors1, torch.Tensor)
        tensors2 = collect_recursively(tensors2, torch.Tensor)
        assert len(tensors1) == len(tensors2)
        return all(torch.equal(t1, t2) for t1, t2 in zip(tensors1, tensors2))

    @staticmethod
    def restore_tensors_recursively(src_tensors, tgt_tensors):
        src_tensors = collect_recursively(src_tensors, torch.Tensor)
        tgt_tensors = collect_recursively(tgt_tensors, torch.Tensor)
        assert len(tgt_tensors) == len(tgt_tensors)
        for src, tgt in zip(src_tensors, tgt_tensors):
            src[:] = tgt

    @staticmethod
    def module_forward_tracing_decorator(forward_func):

        def forward(self, *args, **kwargs):
            if Tracer._tracing_enabled:
                Tracer._tracing_enabled = False

                args_inner, kwargs_inner = deepcopy((args, kwargs), memo={id(t): t for t in collect_recursively(args, torch.Tensor)})
                args_clone, kwargs_clone = clone_torch_tensors_recursively((args, kwargs))

                wrapped_op = WrappedOp(self)

                Tracer._parent_list.append(wrapped_op)
                Tracer._tracing_enabled = True
                outputs = forward_func(*args_inner, **kwargs_inner)
                Tracer._tracing_enabled = False
                Tracer._parent_list = Tracer._parent_list[:-1]

                # Protection from external modification
                outputs_clone = clone_torch_tensors_recursively(outputs)

                is_inplace = not Tracer.are_equal((args, kwargs), (args_clone, kwargs_clone))
                # if not is_inplace:
                #     args_clone = args
                #     kwargs_clone = kwargs

                node = PytorchNode(wrapped_op, self.__module__, Tracer._parent_list.copy(), self, args_clone, kwargs_clone, outputs_clone, is_inplace)
                Tracer._node_list.append(node)

                Tracer._tracing_enabled = True
                return outputs
            else:
                outputs = forward_func(*args, **kwargs)
            return outputs


        # def forward(self, *args, **kwargs):
        #     if Tracer._tracing_enabled:
        #         Tracer._tracing_enabled = False
        #
        #         wrapped_op = WrappedOp(self)
        #
        #         args, kwargs = deepcopy((args, kwargs), memo={id(t): t for t in collect_recursively(args, (torch.Tensor, nn.Module))})
        #         args_reserve, kwargs_reserve = clone_torch_tensors_recursively((args, kwargs))
        #
        #         Tracer._parent_list.append(wrapped_op)
        #         Tracer._tracing_enabled = True
        #         outputs = forward_func(*args, **kwargs)
        #         Tracer._tracing_enabled = False
        #         Tracer._parent_list = Tracer._parent_list[:-1]
        #
        #         is_inplace = not Tracer.are_equal((args, kwargs), (args_reserve, kwargs_reserve))
        #         if is_inplace:
        #             print('??? forward inplace', self)
        #             outputs = clone_torch_tensors_recursively(outputs)
        #             Tracer.restore_tensors_recursively((args, kwargs), (args_reserve, kwargs_reserve))
        #             args, kwargs = args_reserve, kwargs_reserve
        #
        #         node = PytorchNode(wrapped_op, self.__module__, Tracer._parent_list.copy(), self, args, kwargs, outputs, is_inplace)
        #         Tracer._node_list.append(node)
        #
        #         Tracer._tracing_enabled = True
        #         return outputs
        #     else:
        #         return forward_func(*args, **kwargs)


        forward.__undecorated_func__ = forward_func
        return forward


    @staticmethod
    def op_tracing_decorator(orig_method, op_cls, module_suffix=None, is_whitelist_op=False):

        def decorator(*args, **kwargs):
            if Tracer._tracing_enabled:
                Tracer._tracing_enabled = False

                wrapped_op = WrappedOp(orig_method)

                args_inner, kwargs_inner = deepcopy((args, kwargs), memo={id(t): t for t in collect_recursively(args, torch.Tensor)})
                args_clone, kwargs_clone = clone_torch_tensors_recursively((args, kwargs))

                num_input_tensors = len(collect_recursively((args, kwargs), torch.Tensor))

                Tracer._parent_list.append(wrapped_op)
                Tracer._tracing_enabled = True
                outputs = orig_method(*args_inner, **kwargs_inner)
                Tracer._tracing_enabled = False
                Tracer._parent_list = Tracer._parent_list[:-1]

                num_output_tensors = len(collect_recursively(outputs, torch.Tensor))

                if is_whitelist_op or (num_input_tensors > 0 and num_output_tensors > 0):
                    module_name = op_cls.__name__ if isinstance(op_cls, types.ModuleType) else f'{op_cls.__module__}.{op_cls.__name__}'
                    if module_suffix:
                        module_name += '.' + module_suffix

                    # Protection from external modification
                    outputs_clone = clone_torch_tensors_recursively(outputs)

                    is_inplace = not Tracer.are_equal((args, kwargs), (args_clone, kwargs_clone))
                    # if not is_inplace:
                    #     args_clone = args
                    #     kwargs_clone = kwargs

                    node = PytorchNode(wrapped_op, module_name, Tracer._parent_list.copy(), None, args_clone, kwargs_clone, outputs_clone, is_inplace)
                    Tracer._node_list.append(node)

                Tracer._tracing_enabled = True
            else:
                outputs = orig_method(*args, **kwargs)
            return outputs


        # def decorator(*args, **kwargs):
        #     if Tracer._tracing_enabled:
        #         Tracer._tracing_enabled = False
        #
        #         wrapped_op = WrappedOp(orig_method)
        #
        #         args, kwargs = deepcopy((args, kwargs), memo={id(t): t for t in collect_recursively(args, (torch.Tensor, nn.Module))})
        #         args_reserve, kwargs_reserve = clone_torch_tensors_recursively((args, kwargs))
        #
        #         num_input_tensors = len(collect_recursively((args, kwargs), torch.Tensor))
        #
        #         Tracer._parent_list.append(wrapped_op)
        #         Tracer._tracing_enabled = True
        #         outputs = orig_method(*args, **kwargs)
        #         Tracer._tracing_enabled = False
        #         Tracer._parent_list = Tracer._parent_list[:-1]
        #
        #         num_output_tensors = len(collect_recursively(outputs, torch.Tensor))
        #
        #         is_inplace = not Tracer.are_equal((args, kwargs), (args_reserve, kwargs_reserve))
        #         if is_inplace:
        #             print('!!! op inplace', orig_method)
        #             outputs = clone_torch_tensors_recursively(outputs)
        #             Tracer.restore_tensors_recursively((args, kwargs), (args_reserve, kwargs_reserve))
        #             args, kwargs = args_reserve, kwargs_reserve
        #
        #         if is_whitelist_op or (num_input_tensors > 0 and num_output_tensors > 0):
        #             module_name = op_cls.__name__ if isinstance(op_cls, types.ModuleType) else f'{op_cls.__module__}.{op_cls.__name__}'
        #             if module_suffix:
        #                 module_name += '.' + module_suffix
        #
        #             node = PytorchNode(wrapped_op, module_name, Tracer._parent_list.copy(), None, args, kwargs, outputs, is_inplace)
        #             Tracer._node_list.append(node)
        #
        #         Tracer._tracing_enabled = True
        #         return outputs
        #     else:
        #         return orig_method(*args, **kwargs)


        decorator.__undecorated_func__ = orig_method
        return decorator

    @staticmethod
    def op_tracing_decorator_for_class(op_cls):
        for name, method in inspect.getmembers(op_cls):
            if not (inspect.ismethod(method) or inspect.isfunction(method) or inspect.isbuiltin(method) or inspect.isroutine(method)) \
                    or method in Tracer.op_blacklist:
                continue

            if not Tracer.is_decorated(method):
                decorated = Tracer.op_tracing_decorator(method, op_cls, is_whitelist_op=False)
                setattr(op_cls, name, decorated)
        return op_cls

    @staticmethod
    def decorate_all():
        Tracer.decorate_module()
        Tracer.decorate_ops()

    @staticmethod
    def decorate_module():
        def decorated_new(cls, *args, **kwargs):
            self = object.__new__(cls)
            if not Tracer.is_decorated(self.forward):
                self.forward = types.MethodType(Tracer.module_forward_tracing_decorator(self.forward), self)
            return self
        nn.Module.__new__ = decorated_new

    @staticmethod
    def decorate_ops():
        torch.jit._state.disable()

        for op_cls in Tracer.op_tracing_classes:
            Tracer.op_tracing_decorator_for_class(op_cls)

        for op_cls, op in Tracer.op_whitelist_dict.items():
            if not Tracer.is_decorated(op):
                decorated = Tracer.op_tracing_decorator(op, op_cls, is_whitelist_op=True)
                setattr(op_cls, op.__name__, decorated)

    @staticmethod
    def trace(module_or_function: Union[nn.Module, Callable], args, kwargs) -> PytorchNodeHierarchy:

        ### Module tracing routines
        def apply_module_tracing_recursively(module):
            for child in module.children():
                apply_module_tracing_recursively(child)

            if not Tracer.is_decorated(module.forward):
                module.forward = types.MethodType(Tracer.module_forward_tracing_decorator(module.forward), module)
            return module

        ### Initiate tracing
        Tracer.decorate_all()

        Tracer._tracing_enabled = True
        Tracer._node_list = []
        Tracer._parent_list = []
        if isinstance(module_or_function, nn.Module):
            apply_module_tracing_recursively(module_or_function)
        else:
            module_or_function = Tracer.traceable()(module_or_function)
        with torch.no_grad():
            module_or_function(*args, **kwargs)
        Tracer._tracing_enabled = False

        hierarchy = Tracer.build_hierarchy(Tracer._node_list)
        return hierarchy

    @staticmethod
    def build_hierarchy(node_list: Collection[PytorchNode]) -> PytorchNodeHierarchy:

        def build(node_list: Collection[PytorchNode]) -> List[PytorchNodeHierarchy]:
            hierarchies = []
            while len(node_list) > 0:
                node = node_list.pop(len(node_list) - 1)
                child_nodes = [c for c in node_list if node.wrapped_op in c.parent_list]
                node_list = [n for n in node_list if n not in child_nodes]

                child_hierarchies = build(child_nodes)
                hierarchy = PytorchNodeHierarchy(node, child_hierarchies)
                hierarchies.append(hierarchy)
            return list(reversed(hierarchies))

        hierarchies = build(node_list)
        # assert len(hierarchies) == 1
        return hierarchies[-1]
