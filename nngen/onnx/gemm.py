from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import collections

import nngen.storage as storage
import nngen.operator as operator
import nngen.dtype_list as dtype_list

from . import util


def Gemm(visitor, node,
         batchnorm_scale=None, batchnorm_bias=None, act_func=None):

    # input, filter
    srcs = []

    for src in node.input:
        src_obj = visitor.visit(src)
        srcs.append(src_obj)

    input = srcs[0]
    filter = srcs[1]

    bias = srcs[2] if len(srcs) > 2 else None

    name = util.get_name(node)

    scale_name = '_'.join(['onnx', name, 'gemm.scale'])
    scale_width = filter.dtype.width
    scale_dtype = dtype_list.dtype_int(scale_width, signed=True)
    scale_shape = batchnorm_scale.shape if batchnorm_scale is not None else (1,)
    scale = storage.variable(dtype=scale_dtype, shape=scale_shape, name=scale_name)
    scale_value = batchnorm_scale if batchnorm_scale is not None else [1]
    scale.set_value(scale_value)
    visitor.variables[scale_name] = scale

    if bias is None and batchnorm_bias is not None:
        bias_name = '_'.join(['onnx', name, 'gemm.bias'])
        bias_width = filter.dtype.width
        bias_dtype = dtype_list.dtype_int(bias_width, signed=True)
        bias_shape = batchnorm_bias.shape
        bias = storage.variable(dtype=bias_dtype, shape=bias_shape, name=bias_name)
        bias_value = batchnorm_bias / batchnorm_scale
        bias.set_value(bias_value)
        visitor.variables[bias_name] = bias

    elif bias is not None and batchnorm_bias is not None:
        bias_value = batchnorm_bias / batchnorm_scale + bias.value
        bias.set_value(bias_value)

    #rshift_out_name = '_'.join(['onnx, name, 'gemm.rshift_out'])
    #rshift_out_width = filter.dtype.width
    #rshift_out_dtype = dtype_list.dtype_int(rshift_out_width, signed=False)
    #rshift_out_shape = (1,)
    #rshift_out = storage.variable(dtype=scale_dtype, shape=scale_shape, name=rshift_out_name)
    #visitor.variables[rshift_out_name] = rshift_out
    rshift_out = 0

    if name in visitor.value_dtypes:
        dtype = visitor.value_dtypes[name]
    else:
        dtype = visitor.default_operator_dtype

    if dtype.width >= 16:
        sum_dtype = dtype_list.dtype_int(dtype.width * 4)
    else:
        sum_dtype = dtype_list.int32

    args = [input, filter]

    kwargs = collections.OrderedDict()
    kwargs['bias'] = bias
    kwargs['scale'] = scale
    kwargs['transposed_a'] = False
    kwargs['transposed_b'] = True
    kwargs['rshift_out'] = rshift_out
    kwargs['act_func'] = act_func
    kwargs['dtype'] = dtype
    kwargs['sum_dtype'] = sum_dtype
    kwargs['name'] = name

    c = operator.matmul(*args, **kwargs)

    return c
