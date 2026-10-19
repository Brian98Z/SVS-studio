// JSON config and DSP equations shared with inference_service/mixer.py.
export const defaultMixerConfig = () => ({ master: { volume: 1, effects: [] }, buses: [] })

export function equalPowerPan(mono, pan = 0) {
  if (!Number.isFinite(pan) || pan < -1 || pan > 1) throw new RangeError('pan must be between -1 and 1')
  const a = (pan + 1) * Math.PI / 4, out = [new Float32Array(mono.length), new Float32Array(mono.length)]
  const left = pan === 1 ? 0 : pan === -1 ? 1 : Math.cos(a), right = pan === -1 ? 0 : pan === 1 ? 1 : Math.sin(a)
  for (let i = 0; i < mono.length; i++) { out[0][i] = mono[i] * left; out[1][i] = mono[i] * right }
  return out
}

function coeff(hz, rate, low) {
  const w=2*Math.PI*Math.min(hz,rate*.45)/rate,c=Math.cos(w),s=Math.sin(w),a=s/Math.sqrt(2)
  const d=1+a; return [(low?(1-c)/2:(1+c)/2)/d,(low?1-c:-(1+c))/d,(low?(1-c)/2:(1+c)/2)/d,-2*c/d,(1-a)/d]
}
function filterBlock(x, state, rate, hz, low) {
  const [b0,b1,b2,a1,a2]=coeff(hz,rate,low), y=new Float32Array(x.length); let {x1=0,x2=0,y1=0,y2=0}=state
  for(let i=0;i<x.length;i++){const v=x[i],o=b0*v+b1*x1+b2*x2-a1*y1-a2*y2;y[i]=o;x2=x1;x1=v;y2=y1;y1=o}
  Object.assign(state,{x1,x2,y1,y2}); return y
}
export function processStereo(left,right,effects,rate,state={},pan=0,monoInput=false) {
  left=new Float32Array(left);right=new Float32Array(right)
  if(monoInput){const a=(pan+1)*Math.PI/4,gl=pan===1?0:pan===-1?1:Math.cos(a),gr=pan===-1?0:pan===1?1:Math.sin(a);for(let i=0;i<left.length;i++){const mono=left[i];left[i]=mono*gl;right[i]=mono*gr}}
  else {const gl=pan===1?0:pan<=0?1:Math.cos(pan*Math.PI/2),gr=pan===-1?0:pan>=0?1:Math.cos(-pan*Math.PI/2);for(let i=0;i<left.length;i++){left[i]*=gl;right[i]*=gr}}
  for(let k=0;k<(effects||[]).length;k++){
    const e=effects[k];if(!e.enabled)continue
    const st=state[k]||(state[k]={})
    if(e.type==='eq3') for(let c=0;c<2;c++){
      const x=c?right:left,lo=filterBlock(x,st[`lo${c}`]||(st[`lo${c}`]={}),rate,e.low_hz,true),below=filterBlock(x,st[`mid${c}`]||(st[`mid${c}`]={}),rate,e.high_hz,true),dst=c?right:left
      const gl=10**(e.low_db/20),gm=10**(e.mid_db/20),gh=10**(e.high_db/20)
      for(let i=0;i<x.length;i++)dst[i]=lo[i]*gl+(below[i]-lo[i])*gm+(x[i]-below[i])*gh
    } else if(e.type==='reverb'){
      const delay=[Math.max(1,Math.floor(rate*(.035+.11*e.room))),Math.max(1,Math.floor(rate*(.035+.11*e.room)*1.08))],decay=.25+.65*e.room
      for(let c=0;c<2;c++){const xs=st[`x${c}`]||(st[`x${c}`]=new Float32Array(delay[c])),ys=st[`y${c}`]||(st[`y${c}`]=new Float32Array(delay[c])),x=c?right:left;let idx=st[`idx${c}`]||0
        for(let i=0;i<x.length;i++){const wet=xs[idx]+ys[idx]*decay;xs[idx]=x[i];ys[idx]=wet;x[i]=x[i]*(1-e.wet)+wet*e.wet*.35;idx=(idx+1)%xs.length}st[`idx${c}`]=idx}
    } else if(e.type==='compressor'){
      const threshold=10**(e.threshold_db/20),attack=Math.exp(-1/(rate*e.attack_ms*.001)),release=Math.exp(-1/(rate*e.release_ms*.001));let env=st.env||0
      for(let i=0;i<left.length;i++){const peak=Math.max(Math.abs(left[i]),Math.abs(right[i])),a=peak>env?attack:release;env=a*env+(1-a)*peak;if(env>threshold){const g=threshold*(env/threshold)**(1/e.ratio)/env;left[i]*=g;right[i]*=g}}st.env=env
    } else throw new TypeError(`Unknown effect: ${e.type}`)
  }
  return [left,right]
}

export const WORKLET_PROCESSOR_NAME='svs-mixer-fx'
export const workletProcessorSource = `function coeff(hz,rate,low){const w=2*Math.PI*Math.min(hz,rate*.45)/rate,c=Math.cos(w),s=Math.sin(w),a=s/Math.sqrt(2),d=1+a;return[(low?(1-c)/2:(1+c)/2)/d,(low?1-c:-(1+c))/d,(low?(1-c)/2:(1+c)/2)/d,-2*c/d,(1-a)/d]}
function filterBlock(x,state,rate,hz,low){const [b0,b1,b2,a1,a2]=coeff(hz,rate,low),y=new Float32Array(x.length);let{x1=0,x2=0,y1=0,y2=0}=state;for(let i=0;i<x.length;i++){const v=x[i],o=b0*v+b1*x1+b2*x2-a1*y1-a2*y2;y[i]=o;x2=x1;x1=v;y2=y1;y1=o}Object.assign(state,{x1,x2,y1,y2});return y}
function processStereo(left,right,effects,rate,state,pan,mono){left=new Float32Array(left);right=new Float32Array(right);let gl,gr;if(mono){const a=(pan+1)*Math.PI/4;gl=pan===1?0:pan===-1?1:Math.cos(a);gr=pan===-1?0:pan===1?1:Math.sin(a)}else{gl=pan===1?0:pan<=0?1:Math.cos(pan*Math.PI/2);gr=pan===-1?0:pan>=0?1:Math.cos(-pan*Math.PI/2)}for(let i=0;i<left.length;i++){const x=left[i];if(mono){left[i]=x*gl;right[i]=x*gr}else{left[i]*=gl;right[i]*=gr}}for(let k=0;k<effects.length;k++){const e=effects[k];if(!e.enabled)continue;const st=state[k]||(state[k]={});if(e.type==='eq3')for(let c=0;c<2;c++){const x=c?right:left,lo=filterBlock(x,st['lo'+c]||(st['lo'+c]={}),rate,e.low_hz,true),below=filterBlock(x,st['mid'+c]||(st['mid'+c]={}),rate,e.high_hz,true),dst=c?right:left,gl=10**(e.low_db/20),gm=10**(e.mid_db/20),gh=10**(e.high_db/20);for(let i=0;i<x.length;i++)dst[i]=lo[i]*gl+(below[i]-lo[i])*gm+(x[i]-below[i])*gh}else if(e.type==='reverb'){const delay=[Math.max(1,Math.floor(rate*(.035+.11*e.room))),Math.max(1,Math.floor(rate*(.035+.11*e.room)*1.08))],decay=.25+.65*e.room;for(let c=0;c<2;c++){const xs=st['x'+c]||(st['x'+c]=new Float32Array(delay[c])),ys=st['y'+c]||(st['y'+c]=new Float32Array(delay[c])),x=c?right:left;let idx=st['idx'+c]||0;for(let i=0;i<x.length;i++){const wet=xs[idx]+ys[idx]*decay;xs[idx]=x[i];ys[idx]=wet;x[i]=x[i]*(1-e.wet)+wet*e.wet*.35;idx=(idx+1)%xs.length}st['idx'+c]=idx}}else if(e.type==='compressor'){const threshold=10**(e.threshold_db/20),attack=Math.exp(-1/(rate*e.attack_ms*.001)),release=Math.exp(-1/(rate*e.release_ms*.001));let env=st.env||0;for(let i=0;i<left.length;i++){const peak=Math.max(Math.abs(left[i]),Math.abs(right[i])),a=peak>env?attack:release;env=a*env+(1-a)*peak;if(env>threshold){const g=threshold*(env/threshold)**(1/e.ratio)/env;left[i]*=g;right[i]*=g}}st.env=env}else throw new TypeError('Unknown effect: '+e.type)}return[left,right]}
class SvsMixerFx extends AudioWorkletProcessor{constructor(){super();this.fx=[];this.state={};this.pan=0;this.mono=false;this.port.onmessage=e=>{if(e.data.type==='effects'){this.fx=e.data.effects||[];this.state={}}else if(e.data.type==='pan'){this.pan=e.data.pan;this.mono=!!e.data.monoInput}}}process(inputs,outputs){const o=outputs[0];if(!o||!o.length)return true;const i=inputs[0],n=o[0].length,l=i?.[0]||new Float32Array(n),r=this.mono?(i?.[0]||new Float32Array(n)):(i?.[1]||new Float32Array(n)),p=processStereo(l,r,this.fx,sampleRate,this.state,this.pan,this.mono);for(let c=0;c<o.length;c++)o[c].set(p[Math.min(c,1)]);return true}}
registerProcessor('svs-mixer-fx',SvsMixerFx);`
