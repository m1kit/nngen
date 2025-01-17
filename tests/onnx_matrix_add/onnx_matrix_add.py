from __future__ import absolute_import
from __future__ import print_function

import os
import sys
import functools
import math
import numpy as np

import torch
import torchvision
import torchvision.transforms as transforms
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd

# the next line can be removed after installation
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import nngen as ng

from veriloggen import *
import veriloggen.thread as vthread
import veriloggen.types.axi as axi


class MatrixAdd(nn.Module):
    def __init__(self):
        super(MatrixAdd, self).__init__()

    def forward(self, x, y):
        z = torch.add(x, y)
        return z


def run(a_shape=(7, 15), b_shape=(7, 15),
        a_dtype=ng.int32, b_dtype=ng.int32, c_dtype=ng.int32,
        par=1, axi_datawidth=32, silent=False,
        filename=None, simtype='iverilog', outputfile=None):

    # model definition
    model = MatrixAdd()

    # Pytorch to ONNX
    onnx_filename = 'onnx_matrix_add.onnx'
    dummy_a = torch.randn(*a_shape)
    dummy_b = torch.randn(*b_shape)
    dummy_inputs = (dummy_a, dummy_b)
    input_names = ['a', 'b']
    output_names = ['c']
    model.eval()
    torch.onnx.export(model, dummy_inputs, onnx_filename,
                      input_names=input_names, output_names=output_names)

    # ONNX to NNgen
    value_dtypes = {'a': a_dtype,
                    'b': b_dtype,
                    'c': c_dtype}

    (outputs, placeholders, variables,
     constants, operators) = ng.from_onnx(onnx_filename,
                                          value_dtypes=value_dtypes,
                                          default_placeholder_dtype=ng.int32,
                                          default_variable_dtype=ng.int32,
                                          default_constant_dtype=ng.int32,
                                          default_operator_dtype=ng.int32,
                                          default_scale_dtype=ng.int32,
                                          default_bias_dtype=ng.int32,
                                          disable_fusion=False)

    # set attribute
    for op in operators.values():
        if isinstance(op, ng.add):
            op.attribute(par=par)

    # create target hardware
    a = placeholders['a']
    b = placeholders['b']
    c = outputs['c']

    targ = ng.to_veriloggen([c], 'onnx_matrix_add', silent=silent,
                            config={'maxi_datawidth': axi_datawidth})

    # verification data
    va = np.arange(a.length, dtype=np.int64).reshape(a.shape) % [5]
    vb = (np.arange(b.length, dtype=np.int64).reshape(b.shape) + [100]) % [6]

    eval_outs = ng.eval([c], a=va, b=vb)
    vc = eval_outs[0]

    # exec on pytorch
    model_a = va.astype(np.float32)
    model_b = vb.astype(np.float32)
    if a.perm is not None:
        model_a = np.transpose(model_a, a.reversed_perm)
    if b.perm is not None:
        model_b = np.transpose(model_b, b.reversed_perm)

    model.eval()
    model_c = model(torch.from_numpy(model_a), torch.from_numpy(model_b)).detach().numpy()
    if a.perm is not None:
        model_c = np.transpose(model_c, a.perm)
    scaled_model_c = model_c * c.scale_factor

    c_diff = vc - scaled_model_c
    c_err = c_diff / (scaled_model_c + 0.00000001)
    max_c_err = np.max(np.abs(c_err))

    # if max_c_err > 0.1:
    #    raise ValueError("too large output error: %f > 0.1" % max_c_err)

    # to memory image
    param_data = ng.export_ndarray([c])
    param_bytes = len(param_data)

    variable_addr = int(math.ceil(max(a.addr + a.memory_size,
                                      b.addr + b.memory_size) / 4096)) * 4096
    check_addr = int(math.ceil((variable_addr + param_bytes) / 4096)) * 4096
    tmp_addr = int(math.ceil((check_addr + c.memory_size) / 4096)) * 4096

    memimg_datawidth = 32
    mem = np.zeros([1024 * 1024 * 8 // (memimg_datawidth // 8)], dtype=np.int64)
    mem = mem + [100]

    # placeholder
    axi.set_memory(mem, va, memimg_datawidth,
                   a_dtype.width, a.addr,
                   max(int(math.ceil(axi_datawidth / a_dtype.width)), par))
    axi.set_memory(mem, vb, memimg_datawidth,
                   b_dtype.width, b.addr,
                   max(int(math.ceil(axi_datawidth / b_dtype.width)), par))

    # parameters (variable and constant)
    axi.set_memory(mem, param_data, memimg_datawidth,
                   8, variable_addr)

    # verification data
    axi.set_memory(mem, vc, memimg_datawidth,
                   c_dtype.width, check_addr,
                   max(int(math.ceil(axi_datawidth / c_dtype.width)), par))

    # test controller
    m = Module('test')
    params = m.copy_params(targ)
    ports = m.copy_sim_ports(targ)
    clk = ports['CLK']
    resetn = ports['RESETN']
    rst = m.Wire('RST')
    rst.assign(Not(resetn))

    # AXI memory model
    if outputfile is None:
        outputfile = os.path.splitext(os.path.basename(__file__))[0] + '.out'

    memimg_name = 'memimg_' + outputfile

    memory = axi.AxiMemoryModel(m, 'memory', clk, rst,
                                datawidth=axi_datawidth,
                                memimg=mem, memimg_name=memimg_name,
                                memimg_datawidth=memimg_datawidth)
    memory.connect(ports, 'maxi')

    # AXI-Slave controller
    _saxi = vthread.AXIMLite(m, '_saxi', clk, rst, noio=True)
    _saxi.connect(ports, 'saxi')

    # timer
    time_counter = m.Reg('time_counter', 32, initval=0)
    seq = Seq(m, 'seq', clk, rst)
    seq(
        time_counter.inc()
    )

    num_rep = functools.reduce(lambda x, y: x * y, c.shape[:-1], 1)

    def ctrl():
        for i in range(100):
            pass

        ng.sim.set_global_addrs(_saxi, tmp_addr)

        start_time = time_counter.value
        ng.sim.start(_saxi)

        print('# start')

        ng.sim.wait(_saxi)
        end_time = time_counter.value

        print('# end')
        print('# execution cycles: %d' % (end_time - start_time))

        # verify
        ok = True
        for i in range(num_rep):
            for j in range(c.shape[-1]):
                orig = memory.read_word(i * c.aligned_shape[-1] + j,
                                        c.addr, c_dtype.width)
                check = memory.read_word(i * c.aligned_shape[-1] + j,
                                         check_addr, c_dtype.width)

                if vthread.verilog.NotEql(orig, check):
                    print('NG', i, j, orig, check)
                    ok = False
                # else:
                #    print('OK', i, j, orig, check)

        if ok:
            print('# verify: PASSED')
        else:
            print('# verify: FAILED')

        vthread.finish()

    th = vthread.Thread(m, 'th_ctrl', clk, rst, ctrl)
    fsm = th.start()

    uut = m.Instance(targ, 'uut',
                     params=m.connect_params(targ),
                     ports=m.connect_ports(targ))

    # simulation.setup_waveform(m, uut)
    simulation.setup_clock(m, clk, hperiod=5)
    init = simulation.setup_reset(m, resetn, m.make_reset(), period=100, polarity='low')

    init.add(
        Delay(1000000),
        Systask('finish'),
    )

    # output source code
    if filename is not None:
        m.to_verilog(filename)

    # run simulation
    sim = simulation.Simulator(m, sim=simtype)
    rslt = sim.run(outputfile=outputfile)
    lines = rslt.splitlines()
    if simtype == 'verilator' and lines[-1].startswith('-'):
        rslt = '\n'.join(lines[:-1])
    return rslt


if __name__ == '__main__':
    rslt = run(silent=False, filename='tmp.v')
    print(rslt)
