from __future__ import annotations
from typing import TYPE_CHECKING

from time import perf_counter
from collections import namedtuple
from abc import ABC, abstractmethod
from operator import attrgetter, mul
from functools import reduce, partial

import numpy as np
import torch.nn as nn
from pympler.asizeof import asizeof
from torch import no_grad, Tensor
from torch.cuda import Event as cuda_event
from torch.cuda import synchronize as cuda_sync

from torchmeter.unit import auto_unit
from torchmeter.unit import CountUnit, BinaryUnit, TimeUnit, SpeedUnit

if TYPE_CHECKING:
    from typing import Any, Callable, Dict, List
    from typing import Optional, Tuple, Union, Type, NamedTuple, Sequence

    from tqdm import tqdm
    from numpy.typing import NDArray
    from torch import device as tc_device
    from torch.utils.hooks import RemovableHandle

    from torchmeter.engine import OperationNode

    UNIT_TYPE = Optional[Union[Type[CountUnit], 
                               Type[BinaryUnit], 
                               Type[TimeUnit], 
                               Type[SpeedUnit]]]

    SEQ_DATA = NDArray[Union[np.int_, np.float_]]

    FLOAT = Union[float, np.float_]
    SEQ_FUNC = Callable[[SEQ_DATA], FLOAT]

__all__ = ["ParamsMeter", "CalMeter", "MemMeter", "ITTPMeter"]

class UpperLinkData:

    __slots__ = ['val', 'none_str', 'access_cnt',
                 '__parent_data', '__unit_sys']

    def __init__(self, 
                 val:int=0, parent_data:Optional[UpperLinkData]=None,
                 unit_sys:UNIT_TYPE=None,
                 none_str:str='-') -> None:
        self.val = val
        self.__parent_data = parent_data    
        self.__unit_sys = unit_sys
        self.access_cnt = 1
        self.none_str = none_str # Use when there is a "None" in the column where this data is located while rendering the table.
    
    @property
    def raw_data(self) -> float:
        return float(self.val)
    
    def __iadd__(self, other) -> UpperLinkData:
        self.val += other
        self.__upper_update(other)
        # self.access_cnt += 1
        return self
    
    def __upper_update(self, other:int) -> None:
        if self.__parent_data is not None:
            self.__parent_data += other
    
    def __repr__(self) -> str:
        if self.__unit_sys is not None:
            base = auto_unit(self.val/self.access_cnt, self.__unit_sys)
        else:
            base = str(self.val/self.access_cnt)
        return base + (f" [dim](×{self.access_cnt})[/]" if self.access_cnt > 1 else "")

class MetricsData:

    __slots__ = ['vals', 'reduce_func',
                 '__unit_sys', 'none_str']

    def __init__(self, 
                 reduce_func:Optional[SEQ_FUNC]=np.mean, 
                 unit_sys:UNIT_TYPE=CountUnit,
                 none_str:str='-') -> None:
        self.vals:SEQ_DATA = np.array([])
        self.reduce_func = reduce_func if reduce_func is not None else np.mean
        self.__unit_sys = unit_sys
        self.none_str = none_str

    @property
    def metrics(self) -> FLOAT:
        return self.reduce_func(self.vals) if self.vals.any() else 0.
    
    @property
    def iqr(self) -> FLOAT:
        if self.vals.any():
            return np.percentile(self.vals, 75) - np.percentile(self.vals, 25)
        else:
            return 0.
    
    @property
    def val(self) -> Tuple[FLOAT, FLOAT]:
        return self.metrics, self.iqr
    
    @property
    def raw_data(self) -> FLOAT:
        return self.metrics
    
    def append(self, new_val:Any) -> None:
        self.vals = np.append(self.vals, new_val)

    def clear(self) -> None:
        self.vals = np.array([])

    def __repr__(self) -> str:
        if self.__unit_sys is not None:
            return f"{auto_unit(self.metrics, self.__unit_sys)}" + ' ± ' + \
                   f"{auto_unit(self.iqr, self.__unit_sys)}"
        else:
            return f"{self.metrics:.2f} ± {self.iqr:.2f}"

class Statistics(ABC):

    detail_val_container: NamedTuple
    overview_val_container: NamedTuple

    def __new__(cls, *args, **kwargs):
        if not hasattr(cls, 'detail_val_container'):
            raise AttributeError(f"Class '{cls.__name__}' must have the class attribute 'detail_val_container', which should be a NamedTuple")
        if not hasattr(cls, 'overview_val_container'):
            raise AttributeError(f"Class '{cls.__name__}' must have the class attribute 'overview_val_container', which should be a NamedTuple")
        return super().__new__(cls)        

    @property
    @abstractmethod
    def name(self) -> str:
        """The registered name of the statistics in OperationNode"""
        ...

    @property
    @abstractmethod
    def val(self) -> NamedTuple:
        """A namedtuple which contains all the necessary information of the statistics"""
        ...
    
    @property
    @abstractmethod
    def detail_val(self) -> List[NamedTuple]:
        """The list of namedtuple which will be rendered in table"""
        ...

    @property
    @abstractmethod
    def crucial_data(self) -> Dict[str, str]:
        """The dict of crucial data of the statistics, used in profile footer"""
        ...

    @abstractmethod
    def measure(self, *args, **kwargs):
        """To measure the statistics"""
        ...

    @property
    def tb_fields(self) -> Tuple[str,...]:
        return self.detail_val_container._fields
    
    @property
    def ov_fields(self) -> Tuple[str,...]:
        return self.overview_val_container._fields
    
    def init_linkdata(self,
                      attr_name:str,
                      init_val:int=0,
                      opparent:Optional[OperationNode]=None,
                      **kwargs) -> UpperLinkData:
        if opparent is None:
            link_data = UpperLinkData(val=init_val, **kwargs)
        else:
            upper_getter = attrgetter(f"{self.name}.{attr_name}")
            link_data = UpperLinkData(val=init_val, 
                                      parent_data=upper_getter(opparent),
                                      **kwargs)
        return link_data

    def __repr__(self) -> str:
        repr_str = self.val.__class__.__name__ + '\n'

        max_len = max(len(f) for f in self.ov_fields)

        for field in self.ov_fields:
            field_val = getattr(self.val, field, 'N/A')
            if isinstance(field_val, UpperLinkData):
                repr_str += '• ' + f"{field.rjust(max_len)} = {field_val.raw_data:.2f} = {field_val}\n"
            else:
                repr_str += '• ' + f"{field.rjust(max_len)} = {field_val}\n"

        return repr_str

class ParamsMeter(Statistics):

    detail_val_container = namedtuple(      # type: ignore
        typename='Params_INFO', 
        field_names=['Operation_Id', 'Operation_Name', 'Operation_Type', 
                     'Param_Name', 'Requires_Grad', 'Numeric_Num'],
        defaults=[None]*6                   # type: ignore
    )
    
    overview_val_container = namedtuple(    # type: ignore
        typename='Params_INFO', 
        field_names=['Operation_Id', 'Operation_Name', 'Operation_Type', 
                     'Total_Params', 'Learnable_Params'],
        defaults=[None]*5                   # type: ignore
    )

    def __init__(self, opnode: OperationNode) -> None:
        self._opnode = opnode
        self._model:nn.Module = opnode.operation
        
        self.__stat_ls:List[NamedTuple] = [] # record all parameters' information
        self.is_measured = False # used for cache

        _opparent:Optional[OperationNode] = opnode.parent
        self.__RegNum = self.init_linkdata(attr_name='RegNum', init_val=0, opparent=_opparent, unit_sys=CountUnit)
        self.__TotalNum = self.init_linkdata(attr_name='TotalNum', init_val=0, opparent=_opparent, unit_sys=CountUnit)

    @property
    def name(self) -> str:
        return 'param'

    @property
    def RegNum(self) -> UpperLinkData :
        return self.__RegNum
    
    @property
    def TotalNum(self) -> UpperLinkData:
        return self.__TotalNum

    @property
    def detail_val(self) -> List[NamedTuple]:
        self.measure()
        return self.__stat_ls
    
    @property
    def val(self) -> NamedTuple:
        self.measure()
        return self.overview_val_container(
            Operation_Id=self._opnode.node_id,   # type: ignore
            Operation_Name=self._opnode.name,    # type: ignore
            Operation_Type=self._opnode.type,    # type: ignore
            Total_Params=self.TotalNum,          # type: ignore
            Learnable_Params=self.RegNum)        # type: ignore

    @property
    def crucial_data(self) -> Dict[str, str]:
        self.measure()
            
        res_dict = {'Learnable Parameters Num': str(self.RegNum),
                    'Total Parameters Num': str(self.TotalNum)}
        max_keylen = max([len(key) for key in res_dict.keys()])
        res_dict = {key.ljust(max_keylen): value for key, value in res_dict.items()}
        return res_dict

    def measure(self) -> None:
        if self.is_measured:
            return
        
        if not self._model._parameters:
            self.__stat_ls.append(self.detail_val_container(            # type: ignore
                Operation_Id=self._opnode.node_id,                      # type: ignore
                Operation_Name=self._opnode.name,                       # type: ignore
                Operation_Type=self._opnode.type,                       # type: ignore
                Numeric_Num=UpperLinkData(val=0, unit_sys=CountUnit))   # type: ignore
            )
        else:
            for param_name, param_val in self._model._parameters.items(): 
                if param_val is None:
                    continue
                p_num = param_val.numel()
                
                p_reg = False
                if param_val.requires_grad:
                    p_reg = True
                    self.__RegNum += p_num

                self.__stat_ls.append(self.detail_val_container(    # type: ignore
                    Operation_Id=self._opnode.node_id,              # type: ignore
                    Operation_Name=self._opnode.name,               # type: ignore
                    Operation_Type=self._opnode.type,               # type: ignore
                    Param_Name=param_name,                          # type: ignore
                    Requires_Grad=p_reg,                            # type: ignore
                    Numeric_Num=UpperLinkData(val=p_num, unit_sys=CountUnit))   # type: ignore
                )
                
                self.__TotalNum += p_num
        
        self.is_measured = True

class CalMeter(Statistics):

    detail_val_container:NamedTuple = namedtuple(       # type: ignore
        typename='Calculation_INFO', 
        field_names=['Operation_Id', 'Operation_Name', 'Operation_Type', 
                     'Kernel_Size', 'Bias', 
                     'Input', 'Output', 
                     'MACs', 'FLOPs'],
        defaults=(None,)*9)                             # type: ignore
    
    overview_val_container:NamedTuple = namedtuple(     # type: ignore
        typename='Calculation_INFO', 
        field_names=['Operation_Id', 'Operation_Type', 'Operation_Name', 
                     'MACs', 'FLOPs'],
        defaults=(None,)*5)                             # type: ignore

    def __init__(self, opnode: OperationNode) -> None:
        self._opnode = opnode
        self._model:nn.Module = opnode.operation
        
        self.__stat_ls:List[NamedTuple] = [] # record the flops and macs information of each operation
        self.is_measured = False 

        _opparent:Optional[OperationNode] = opnode.parent
        self.__Macs = self.init_linkdata(attr_name='Macs', init_val=0, opparent=_opparent, 
                                         unit_sys=CountUnit, none_str='Not Supported')
        self.__Flops = self.init_linkdata(attr_name='Flops', init_val=0, opparent=_opparent, 
                                          unit_sys=CountUnit, none_str='Not Supported')

    @property
    def name(self) -> str:
        return 'cal'

    @property
    def Macs(self) -> UpperLinkData :
        return self.__Macs
    
    @property
    def Flops(self) -> UpperLinkData:
        return self.__Flops

    @property
    def detail_val(self) -> List[NamedTuple]:
        self.__is_valid_access()
        return self.__stat_ls
    
    @property
    def val(self) -> NamedTuple:
        self.__is_valid_access()
        return self.overview_val_container(      # type: ignore
            Operation_Id=self._opnode.node_id,   # type: ignore
            Operation_Type=self._opnode.type,    # type: ignore
            Operation_Name=self._opnode.name,    # type: ignore
            MACs=self.Macs,                      # type: ignore
            FLOPs=self.Flops                     # type: ignore
        )

    @property
    def crucial_data(self) -> Dict[str, str]:
        self.__is_valid_access()
        res_dict = {'FLOPs': str(self.Flops),
                    'MACs(aka MACC, MADD)': str(self.Macs)}
        max_keylen = max([len(key) for key in res_dict.keys()])
        res_dict = {key.ljust(max_keylen): value for key, value in res_dict.items()}
        return res_dict

    def measure(self) -> Optional[RemovableHandle]:
        if self.is_measured:
            return None
        
        hook = self.__regist_hook(self._model) # torch.utils.hooks.RemovableHandle

        self.is_measured = True

        return hook

    def __is_valid_access(self) -> bool:
        if self.is_measured:
            if not (self.Flops.val + self.Macs.val) and \
               not self.__stat_ls and \
               not isinstance(self._model, (nn.ModuleDict, nn.ModuleList)):
                raise RuntimeError("This module might be defined but not explicitly called, so no data is collected.")
        else:
            raise AttributeError("You should never access this property on your own " + \
                                 "before accessing `Meter(your_model).cal`.")
        return True

    def __iopt_repr(self, iopt) -> str:
        repr: Sequence[str]

        if isinstance(iopt, Tensor):
            return str(list(iopt.shape))
        
        elif iopt is None:
            return 'None'

        elif isinstance(iopt, (tuple, list, set)):
            repr = tuple(map(self.__iopt_repr, iopt))
            return '(' + ',\n'.join(repr) + ')' if len(repr) > 1 else repr[0]
        
        elif isinstance(iopt, dict):
            repr = ["{}: {}".format(self.__iopt_repr(k), self.__iopt_repr(v))
                    for k, v in iopt.items()]
            return '{' + ',\n'.join(repr) + '}' if len(repr) > 1 else repr[0]
        
        else:
            return type(iopt).__name__

    def __regist_hook(self, module):
        if not self._opnode.is_leaf:
            h = module.register_forward_hook(self.__container_hook)
            
        elif isinstance(module, (nn.Conv1d, nn.Conv2d, nn.Conv3d)):
            h = module.register_forward_hook(self.__conv_hook)

        elif isinstance(module, (nn.Sigmoid, nn.Tanh, nn.ReLU, nn.ReLU6, nn.SiLU, nn.PReLU, nn.RReLU, nn.LeakyReLU)):
            h = module.register_forward_hook(self.__activate_hook)

        elif isinstance(module, (nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d)):
            h = module.register_forward_hook(self.__BN_hook)

        elif isinstance(module, nn.Linear):
            h = module.register_forward_hook(self.__linear_hook)

        elif isinstance(module, (nn.MaxPool1d, nn.AvgPool1d, nn.MaxPool2d, nn.AvgPool2d, nn.MaxPool3d, nn.AvgPool3d)):
            h = module.register_forward_hook(self.__pool_hook)

        else:
            h = module.register_forward_hook(self.__not_support_hook)

        return h
    
    def __conv_hook(self, module, input, output):
        c_in = input[0].shape[1]
        n = c_in * reduce(mul, module.kernel_size)
        m = output.numel()
        is_bias = 1 if module.bias is not None else 0

        FLOPs = m*(2*n-1+is_bias)
        MACs = m*n
        self.__Macs += MACs
        self.__Flops += FLOPs
        
        if len(self.__stat_ls):
            self.Macs.access_cnt += 1
            self.Flops.access_cnt += 1
        else:
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type,
                Kernel_Size=list(module.kernel_size),
                Bias=bool(is_bias),
                Input=self.__iopt_repr(input),
                Output=self.__iopt_repr(output),
                MACs=self.Macs,
                FLOPs=self.Flops)
            )
    
    def __linear_hook(self, module, input, output):
        k = module.in_features
        l = module.out_features # noqa
        is_bias = 1 if module.bias is not None else 0
        n = k

        FLOPs = l*(2*n-1 + is_bias)
        MACs = l*n
        self.__Macs += MACs
        self.__Flops += FLOPs

        if len(self.__stat_ls):
            self.Macs.access_cnt += 1
            self.Flops.access_cnt += 1
        else:
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type,
                Bias=bool(is_bias),
                Input=self.__iopt_repr(input),
                Output=self.__iopt_repr(output),
                MACs=self.Macs,
                FLOPs=self.Flops)
            )

    def __BN_hook(self, module, input, output):
        FLOPs = 4*input[0].numel()
        MACs = 0.5*FLOPs
        self.__Macs += MACs
        self.__Flops += FLOPs

        if len(self.__stat_ls):
            self.Macs.access_cnt += 1
            self.Flops.access_cnt += 1
        else:
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type,
                Input=self.__iopt_repr(input),
                Output=self.__iopt_repr(output),
                MACs=self.Macs,
                FLOPs=self.Flops)
            )

    def __activate_hook(self, module, input, output):
        k = input[0].numel()
        if isinstance(module, (nn.Sigmoid, nn.PReLU, nn.RReLU, nn.LeakyReLU)):
            FLOPs = 4*k
            MACs = 2*k

        elif isinstance(module, nn.Tanh):
            FLOPs = 9*k
            MACs = 5*k

        elif isinstance(module, (nn.ReLU, nn.ReLU6)):
            FLOPs = k
            MACs = k

        else: # SiLU
            FLOPs = 5*k
            MACs = 3*k

        self.__Macs += MACs
        self.__Flops += FLOPs

        if len(self.__stat_ls):
            self.Macs.access_cnt += 1
            self.Flops.access_cnt += 1
        else:
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type,
                Input=self.__iopt_repr(input),
                Output=self.__iopt_repr(output),
                MACs=self.Macs,
                FLOPs=self.Flops)
            )

    def __pool_hook(self, module, input, output):
        k = module.kernel_size
        k = (k,) if isinstance(k, int) else k
        n = reduce(mul, k)-1
        m = output.numel()

        if isinstance(module, (nn.MaxPool1d, nn.MaxPool2d, nn.MaxPool3d)):
            FLOPs = n*m
        else: # avgpool
            FLOPs = (2*n+1)*m
        MACs = n*m

        self.__Macs += MACs
        self.__Flops += FLOPs

        if len(self.__stat_ls):
            self.Macs.access_cnt += 1
            self.Flops.access_cnt += 1
        else:
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type,
                Kernel_Size=list(k) if len(k)>1 else [k[0]]*2,
                Input=self.__iopt_repr(input),
                Output=self.__iopt_repr(output),
                MACs=self.Macs,
                FLOPs=self.Flops)
            )

    def __container_hook(self, module, input, output):
        if len(self.__stat_ls):
            self.Macs.access_cnt += 1
            self.Flops.access_cnt += 1
        else:
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type,
                Input=self.__iopt_repr(input),
                Output=self.__iopt_repr(output),
                MACs=self.Macs,
                FLOPs=self.Flops)
            )

    def __not_support_hook(self, module, input, output):
        if not len(self.__stat_ls):
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type,
                Input=self.__iopt_repr(input),
                Output=self.__iopt_repr(output))
            )

class MemMeter(Statistics):

    detail_val_container:NamedTuple = namedtuple(                         # type: ignore
        typename='Memory_INFO', 
        field_names=['Operation_Id', 'Operation_Name', 'Operation_Type',
                     'Param_Cost', 'Buffer_Cost', 'Output_Cost',  
                     'Total'],
        defaults=(None,)*7)                                               # type: ignore
    
    overview_val_container:NamedTuple = namedtuple(                       # type: ignore
        typename='Memory_INFO', 
        field_names=['Operation_Id', 'Operation_Type', 'Operation_Name',
                     'Param_Cost', 'Buffer_Cost', 'Output_Cost',
                     'Total'],
        defaults=(None,)*7)                                               # type: ignore

    def __init__(self, opnode: OperationNode) -> None:
        self._opnode = opnode
        self._model:nn.Module = opnode.operation
        self.is_inplace:bool = getattr(self._model, 'inplace', False)
        
        self.__stat_ls:List[NamedTuple] = [] # record the flops and macs information of each operation
        self.is_measured = False # used for cache

        _opparent:Optional[OperationNode] = opnode.parent
        self.__ParamCost = self.init_linkdata(attr_name='ParamCost', init_val=0, opparent=_opparent, unit_sys=BinaryUnit)
        self.__BufferCost = self.init_linkdata(attr_name='BufferCost', init_val=0, opparent=_opparent, unit_sys=BinaryUnit)
        self.__OutputCost = self.init_linkdata(attr_name='OutputCost', init_val=0, opparent=_opparent, unit_sys=BinaryUnit)
        self.__TotalCost = self.init_linkdata(attr_name='TotalCost', init_val=0, opparent=_opparent, unit_sys=BinaryUnit)

    @property
    def name(self) -> str:
        return 'mem'

    @property
    def ParamCost(self) -> UpperLinkData :
        return self.__ParamCost
    
    @property
    def BufferCost(self) -> UpperLinkData:
        return self.__BufferCost

    @property
    def OutputCost(self) -> UpperLinkData:
        return self.__OutputCost
    
    @property
    def TotalCost(self) -> UpperLinkData:
        return self.__TotalCost
    
    @property
    def detail_val(self) -> List[NamedTuple]:
        self.__is_valid_access()
        return self.__stat_ls
                
    @property
    def val(self) -> NamedTuple:
        self.__is_valid_access()
        return self.overview_val_container(         # type: ignore
            Operation_Id=self._opnode.node_id,      # type: ignore
            Operation_Type=self._opnode.type,       # type: ignore
            Operation_Name=self._opnode.name,       # type: ignore
            Param_Cost=self.ParamCost,              # type: ignore
            Buffer_Cost=self.BufferCost,            # type: ignore
            Output_Cost=self.OutputCost,            # type: ignore
            Total=self.TotalCost                    # type: ignore
        )

    @property
    def crucial_data(self) -> Dict[str, str]:
        self.__is_valid_access()
        res_dict =  {'[b]Parameters[/] Memory Cost': f"{self.ParamCost}, {self.ParamCost.val*100/self.TotalCost.val:.2f} %",
                     '[b]Buffers[/] Memory Cost': f"{self.BufferCost}, {self.BufferCost.val*100/self.TotalCost.val:.2f} %",
                     '[b]FeatureMap[/] Memory Cost': f"{self.OutputCost}, {self.OutputCost.val*100/self.TotalCost.val:.2f}",
                     '[b]Total Memory Cost[/]': str(self.TotalCost)}
        max_keylen = max([len(key) for key in res_dict.keys()])
        res_dict = {key.ljust(max_keylen): value for key, value in res_dict.items()}
        return res_dict

    def measure(self) -> Optional[RemovableHandle]:
        if self.is_measured:
            return None
        
        hook = self._model.register_forward_hook(self.__hook_func)
        
        self.is_measured = True

        return hook

    def __hook_func(self, module, input, output):
        param_cost = 0 # byte
        for param in module._parameters.values():
            if param is None:
                continue
            param_cost += param.numel() * param.element_size()
        self.__ParamCost += param_cost
        
        buffer_cost = 0 # byte
        for buffer in module.buffers():
            buffer_cost += buffer.numel() * buffer.element_size()
        self.__BufferCost += buffer_cost
        
        opt_cost = 0
        if self._opnode.is_leaf and not self.is_inplace:
            for opt in output:
                if isinstance(opt, Tensor):
                    opt_cost += opt.numel() * opt.element_size() # byte
                elif isinstance(opt, str):
                    opt_cost += opt.__sizeof__()
                else:
                    opt_cost += asizeof(opt)
        self.__OutputCost += opt_cost
        
        if len(self.__stat_ls):
            # duplicated access
            self.OutputCost.access_cnt += 1
            total_cost = opt_cost
        else:
            total_cost = param_cost + buffer_cost + opt_cost
            self.__stat_ls.append(self.detail_val_container(
                Operation_Id=self._opnode.node_id,
                Operation_Name=self._opnode.name,
                Operation_Type=self._opnode.type + ('(inplace)' if self.is_inplace else ''),
                Param_Cost=None if self._opnode.is_leaf and not param_cost else self.ParamCost, 
                Buffer_Cost=None if self._opnode.is_leaf and not buffer_cost else self.BufferCost, 
                Output_Cost=None if self._opnode.is_leaf and not opt_cost else self.OutputCost, 
                Total=None if self._opnode.is_leaf and not total_cost else self.TotalCost)
            )
        
        self.__TotalCost += total_cost

    def __is_valid_access(self) -> bool:
        if self.is_measured:
            if not self.__stat_ls and not isinstance(self._model, (nn.ModuleDict, nn.ModuleList)):
                raise RuntimeError("This module might be defined but not explicitly called, so no data is collected.")
        else:
            raise AttributeError("You should never access this property on your own " + \
                                 "before accessing `Meter(your_model).cal`.")
        return True

class ITTPMeter(Statistics):

    detail_val_container:NamedTuple = namedtuple(                           # type: ignore
        typename='InferTime_Throughput_INFO', 
        field_names=['Operation_Id', 'Operation_Name', 'Operation_Type',
                     'Infer_Time', 'Throughput'],
        defaults=(None,)*5)                                                 # type: ignore
    
    overview_val_container:NamedTuple = namedtuple(                         # type: ignore
        typename='InferTime_Throughput_INFO', 
        field_names=['Operation_Id', 'Operation_Name','Operation_Type',
                     'Infer_Time', 'Throughput'],
        defaults=(None,)*5,)                                                 # type: ignore
                                                                
    def __init__(self, opnode: OperationNode) -> None:
        self._opnode = opnode
        self._model:nn.Module = opnode.operation
        
        self.__stat_ls:List[NamedTuple] = [] # record the inference time and throughput of each operation
        self.is_measured = False

        self.__InferTime = MetricsData(reduce_func=np.median, unit_sys=TimeUnit)
        self.__Throughput = MetricsData(reduce_func=np.median, unit_sys=SpeedUnit)

    @property
    def name(self) -> str:
        return 'ittp'

    @property
    def InferTime(self) -> MetricsData :
        return self.__InferTime
    
    @property
    def Throughput(self) -> MetricsData:
        return self.__Throughput

    @property
    def detail_val(self) -> List[NamedTuple]:
        self.__is_valid_access()
        return self.__stat_ls
    
    @property
    def val(self) -> NamedTuple:
        self.__is_valid_access()
        return self.overview_val_container(         # type: ignore
            Operation_Id=self._opnode.node_id,      # type: ignore
            Operation_Type=self._opnode.type,       # type: ignore
            Operation_Name=self._opnode.name,       # type: ignore
            Infer_Time=self.__InferTime,            # type: ignore
            Throughput=self.__Throughput            # type: ignore
        )

    @property
    def crucial_data(self) -> Dict[str, str]:
        self.__is_valid_access()
        res_dict = {'Inference Elapse': str(self.InferTime),
                    'Throughput': str(self.Throughput)}
        max_keylen = max([len(key) for key in res_dict.keys()])
        res_dict = {key.ljust(max_keylen): value for key, value in res_dict.items()}
        return res_dict

    def measure(self, device:tc_device, repeat:int=50, global_process:Optional[tqdm]=None) -> RemovableHandle:
        
        hook = self._model.register_forward_hook(
            partial(self.__hook_func, 
                    device=device, 
                    repeat=repeat,
                    global_process=global_process)
        )
        
        self.is_measured = True
        
        return hook

    def __hook_func(self, module, input, output, device:tc_device, repeat:int=50, global_process:Optional[tqdm]=None):
        self.__InferTime.clear()
        self.__Throughput.clear()
        self.__stat_ls.clear()
    
        module._forward_hooks.clear()
        module.eval()

        
        start_event = cuda_event(enable_timing=True)
        end_event = cuda_event(enable_timing=True)
        cpu_timer = perf_counter
        gpu_start_timer = start_event.record
        gpu_end_timer = end_event.record

        cuda_sync()  # WAIT FOR GPU SYNC  
        with no_grad():
            for _ in range(repeat):
                start_time = cpu_timer() if device.type == 'cpu' else gpu_start_timer()

                module(*input)

                end_time = cpu_timer() if device.type == 'cpu' else gpu_end_timer()

                if device.type == 'cpu':
                    it = end_time-start_time
                else:
                    cuda_sync()  # WAIT FOR GPU SYNC
                    it = start_event.elapsed_time(end_event)*1e-3 # ms -> s    # type: ignore

                tp = input[0].shape[0]/it
                self.__InferTime.append(it) 
                self.__Throughput.append(tp)

                if global_process is not None:
                    global_process.update(1)

        self.__stat_ls.append(
            self.detail_val_container(Operation_Id=self._opnode.node_id,   # type: ignore
                                      Operation_Name=self._opnode.name,    # type: ignore
                                      Operation_Type=self._opnode.type,    # type: ignore
                                      Infer_Time=self.InferTime,           # type: ignore
                                      Throughput=self.Throughput)          # type: ignore
        )

    def __is_valid_access(self) -> bool:
        if self.is_measured:
            if not self.__stat_ls and not isinstance(self._model, (nn.ModuleDict, nn.ModuleList)):
                raise RuntimeError("This module might be defined but not explicitly called, so no data is collected.")
        else:
            raise AttributeError("You should never access this property on your own " + \
                                 "before accessing `Meter(your_model).cal`.")
        return True
