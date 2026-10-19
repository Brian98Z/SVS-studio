"""Opt-in real-model smoke. Run directly; not collected as a unit-test model download."""
import base64
import contextlib
import io
import json
import uuid
from pathlib import Path
import numpy as np
from inference_service import contract as C
from inference_service.render_ds import render_track

if __name__ == '__main__':
    import librosa
    import soundfile as sf
    out=Path('C:/Users/Administrator/AppData/Local/Temp/svs-native-controls-smoke')
    out.mkdir(exist_ok=True)
    notes=[{'tick':0,'duration':1920,'key':60,'lyric':'啊','kind':'sung'}]
    tempos=[{'tick':0,'bpm':120}]
    results={}
    for label,curves,note in [
        ('flat',{},notes),
        ('pitch',{'pitch':{'direct':C.encode_f32([300.]*1920)}},notes),
        ('vibrato',{},[{**notes[0],'vibrato':{'depth_cents':80.,'frequency_hz':5.,'delay_ms':300.,'onset_ms':100.,'release_ms':100.}}]),
        ('air',{'air':{'direct':C.encode_f32([.6]*1920)}},notes),
    ]:
        pcm,_=render_track(note,tempos,uuid.uuid4().hex,curves=curves)
        sf.write(out/(label+'.wav'),pcm,24000,subtype='FLOAT')
        f0=librosa.yin(pcm[12000:40000],fmin=200,fmax=450,sr=24000,frame_length=1024,hop_length=120)
        midi=librosa.hz_to_midi(f0)
        results[label]={'frames':len(pcm),'midi_median':float(np.median(midi)), 'midi_spread':float(np.percentile(midi,90)-np.percentile(midi,10)), 'rms':float(np.sqrt(np.mean(pcm**2)))}
    assert abs(results['pitch']['midi_median']-results['flat']['midi_median']-3)<.5,results
    assert results['vibrato']['midi_spread']>results['flat']['midi_spread']+.5,results
    (out/'measurements.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print('REAL_NATIVE_CONTROLS',json.dumps(results))
