"""Offline recognition replay ONLY. Does not send input or report a bot win rate."""
from pathlib import Path
from collections import Counter
import argparse
import json
import time
import cv2
from vision import Vision

def main():
    p=argparse.ArgumentParser();p.add_argument('video',type=Path)
    p.add_argument('--rect',default='666,40,588,968');p.add_argument('--out',type=Path,default=Path('replay_report.json'))
    args=p.parse_args();x,y,w,h=map(int,args.rect.split(','))
    cap=cv2.VideoCapture(str(args.video))
    if not cap.isOpened():raise SystemExit('Cannot open video')
    fps=cap.get(cv2.CAP_PROP_FPS)
    if fps<=0:raise SystemExit('Invalid video frame rate')
    cv2.setNumThreads(1);v=Vision(Path(__file__).resolve().parent/'assets')
    counts=Counter();transitions=[];qte=[];last=None;i=0;start=time.perf_counter()
    while True:
        ok,im=cap.read()
        if not ok:break
        if x<0 or y<0 or x+w>im.shape[1] or y+h>im.shape[0]:raise SystemExit('ROI outside recording')
        t=i/fps;o=v.analyze(im[y:y+h,x:x+w],t);counts[o.state]+=1
        if o.state!=last:transitions.append(dict(t=round(t,4),state=o.state));last=o.state
        if o.state=='QTE':qte.append(dict(t=round(t,4),x=o.x,target=o.target,flash=o.flash))
        i+=1
    cap.release()
    report=dict(scope='OFFLINE recognition of a manually played recording; NOT a live-input test or win-rate estimate.',
                frames=i,fps=fps,processing_seconds=round(time.perf_counter()-start,2),
                state_counts=counts,transitions=transitions,qte_tracking=qte)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('qte_tracking','transitions')},indent=2))
if __name__=='__main__':main()
