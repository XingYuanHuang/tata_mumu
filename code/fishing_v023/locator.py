"""Locate gameplay or result-card geometry. No OCR or fixed desktop coordinates."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import cv2
import numpy as np
from vision import WIDTH, HEIGHT, Vision

@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int
    def grab(self):
        return dict(left=self.left, top=self.top, width=self.width, height=self.height)
    def point(self, x, y):
        return (round(self.left + x * self.width / WIDTH),
                round(self.top + y * self.height / HEIGHT))
    def contained(self, w, h):
        return (self.left >= 0 and self.top >= 0 and
                self.left + self.width <= w and self.top + self.height <= h)
    def translated(self, x, y):
        return Region(self.left+x, self.top+y, self.width, self.height)
    def crop(self, image):
        return image[self.top:self.top+self.height, self.left:self.left+self.width]

@dataclass
class Located:
    region: Region
    score: float
    state: str

class Locator:
    def __init__(self, assets: Path):
        self.assets = assets
        self.title = cv2.imdecode(np.fromfile(str(assets/'title.png'), np.uint8), 1)
        if self.title is None:
            raise RuntimeError('Missing title.png')
        self.title_gray = cv2.cvtColor(self.title, cv2.COLOR_BGR2GRAY)
    def locate(self, image: np.ndarray) -> Located | None:
        anchors=[('title',211,12,163,47,('READY','WAIT','BITE','QTE')),
                 ('loot',151,123,287,68,('REWARD',)),
                 ('record',151,151,300,55,('RECORD',)),
                 ('congrats',145,136,299,78,('RESULT',))]
        for anchor in anchors:
            result=self._locate_anchor(image,anchor)
            if result:return result
        return self._locate_card_layout(image)

    def _locate_anchor(self,image,anchor):
        name,ax,ay,aw,ah,allowed=anchor
        template0=cv2.imdecode(np.fromfile(str(self.assets/(name+'.png')),np.uint8),0)
        h, w = image.shape[:2]
        if w < 230 or h < 420:
            return None
        # A top title is a much more stable anchor than animated water or text OCR.
        max_scale = min(w/WIDTH, h/HEIGHT, 2.6)
        if max_scale < .38:
            return None
        search = cv2.cvtColor(image[:min(h, max(260, int(h*.32)))], cv2.COLOR_BGR2GRAY)
        candidates = []
        def match(scale):
            tw, th = round(aw*scale), round(ah*scale)
            if tw < 45 or th < 12 or tw > search.shape[1] or th > search.shape[0]:
                return
            template = cv2.resize(template0,(tw,th),interpolation=cv2.INTER_AREA if scale<1 else cv2.INTER_LINEAR)
            res = cv2.matchTemplate(search,template,cv2.TM_CCOEFF_NORMED)
            _, score, _, pos = cv2.minMaxLoc(res)
            x,y = pos
            candidates.append((float(score),scale,x,y))
        for scale in np.arange(.38,max_scale+.001,.025):
            match(float(scale))
        match(max_scale)
        if not candidates:
            return None
        coarse = sorted(candidates,reverse=True)[:3]
        for _,scale,_,_ in coarse:
            for fine in np.arange(max(.35,scale-.022),min(max_scale,scale+.022)+.001,.004):
                match(float(fine))
        seen=set(); vision=Vision(self.assets)
        for score,scale,x,y in sorted(candidates,reverse=True)[:18]:
            if score < .70:
                break
            r=Region(round(x-ax*scale),round(y-ay*scale),round(WIDTH*scale),round(HEIGHT*scale))
            if r in seen or not r.contained(w,h):
                continue
            seen.add(r)
            obs=vision.analyze(r.crop(image),0)
            # Require both a title and a known in-game control, not a title alone.
            if obs.state in allowed and (obs.scores.get(name,0)>(.72 if name=='congrats' else .78) or
                    (name in ('congrats','record') and obs.scores.get('card_layout')==1.)):
                return Located(r,score,obs.state)
        return None

    def _locate_card_layout(self, image):
        """Start/resume on any result headline using the card edges as anchor.

        This only runs in the selected emulator window, and validates the kg
        badge + header/footer with Vision before returning any coordinates.
        """
        h,w=image.shape[:2]
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        mask=(cv2.inRange(hsv,(85,75,85),(122,255,255)) |
              cv2.inRange(hsv,(10,75,85),(40,255,255)))
        mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((9,9),np.uint8))
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        vision=Vision(self.assets)
        for c in sorted(contours,key=cv2.contourArea,reverse=True)[:5]:
            x,y,cw,ch=cv2.boundingRect(c)
            scale=cw/WIDTH
            if cw<230 or not .35<cw/max(ch,1)<2.3 or not .38<=scale<=2.6:
                continue
            # Bilateral row support excludes the slanted footer edge.
            left=mask[:,x+round(20*scale):x+round(125*scale)]>0
            right=mask[:,x+round(460*scale):min(w,x+round(568*scale))]>0
            if left.shape[1]==0 or right.shape[1]==0:continue
            rows=(np.minimum(left.mean(1),right.mean(1))>.56).astype(np.uint8)
            rows[:y]=0;rows[y+ch:]=0
            rows=cv2.morphologyEx(rows[:,None],cv2.MORPH_CLOSE,
                                  np.ones((max(3,round(23*scale)),1),np.uint8))[:,0]
            ys=np.flatnonzero(rows)
            groups=np.split(ys,np.where(np.diff(ys)>1)[0]+1)
            groups=[g for g in groups if len(g)>170*scale]
            if not groups:continue
            band=max(groups,key=len)
            origin=round((int(band[-1])+1)-715*scale)
            for dy in (0,-6,6,-12,12):
                rh=round(HEIGHT*scale);rt=origin+round(dy*scale)
                # At a tightly cropped window, antialiasing can shift the
                # inferred edge by a pixel. Clamp only this small edge error.
                if rh<=h and -16*scale<=rt<=h-rh+16*scale:
                    rt=min(max(0,rt),h-rh)
                r=Region(x,rt,cw,rh)
                if not r.contained(w,h):continue
                obs=vision.analyze(r.crop(image),0)
                if obs.state in ('RESULT','RECORD','CONTINUE') and obs.scores.get('card_layout'):
                    return Located(r,float(obs.scores['card_kg']),obs.state)
        return None
