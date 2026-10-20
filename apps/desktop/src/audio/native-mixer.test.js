import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { nativeMixerProcessorSource } from './native-mixer.js'

function worklet() {
  let Processor
  const messages = []
  const context = vm.createContext({ Float32Array, ArrayBuffer, currentTime: 0,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: m => messages.push(m) } } },
    registerProcessor: (_, type) => { Processor = type } })
  vm.runInContext(nativeMixerProcessorSource, context)
  const node = new Processor({ processorOptions: { tracks: ['a'], blockFrames: 256, targetBlocks: 2 } })
  node.port.onmessage({ data: { type: 'arm', startTime: 0 } })
  return { node, messages, render: () => {
    const out = [new Float32Array(128), new Float32Array(128)]
    node.process([], [out])
    return out[0]
  }, push: value => node.receiveOutput({ samples: new Float32Array(512).fill(value).buffer }) }
}

test('underrun inserts silence then resumes queued PCM in order after refill', () => {
  const { render, push, messages } = worklet()
  push(.25); push(.5)
  assert.equal(render()[0], .25)
  assert.equal(render()[0], .25)
  assert.equal(render()[0], .5)
  assert.equal(render()[0], .5)
  assert.equal(render()[0], 0)
  assert.equal(messages.filter(m => m.type === 'underrun').length, 1)
  push(.75)
  assert.equal(render()[0], 0)
  push(1)
  assert.equal(render()[0], .75)
  assert.equal(render()[0], .75)
  assert.equal(render()[0], 1)
})

test('bounded output queue signals overflow without discarding earlier PCM', () => {
  const { render, push, messages } = worklet()
  for (let i = 1; i <= 6; i++) push(i)
  assert.equal(messages.filter(m => m.type === 'overflow').length, 1)
  for (let i = 1; i <= 5; i++) {
    assert.equal(render()[0], i)
    assert.equal(render()[0], i)
  }
})

test('device capture never also plays queued audio through the browser', () => {
  let Processor
  const messages = []
  const context = vm.createContext({ Float32Array, ArrayBuffer, currentTime: 0,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: m => messages.push(m) } } },
    registerProcessor: (_, type) => { Processor = type } })
  vm.runInContext(nativeMixerProcessorSource, context)
  const node = new Processor({ processorOptions: { tracks: ['a'], blockFrames: 128, targetBlocks: 1, captureOnly: true } })
  node.port.onmessage({ data: { type: 'arm', startTime: 0 } })
  node.receiveOutput({ samples: new Float32Array(256).fill(.75).buffer })
  const out = [new Float32Array(128).fill(1), new Float32Array(128).fill(1)]
  node.process([[new Float32Array(128).fill(.25), new Float32Array(128).fill(-.5)]], [out])
  assert.ok(out.every(channel => channel.every(sample => sample === 0)))
  const capture = new Float32Array(messages.find(message => message.type === 'capture').blocks[0])
  assert.equal(capture[0], .25)
  assert.equal(capture[1], -.5)
  assert.equal(messages.some(message => message.type === 'clock'), false)
})
