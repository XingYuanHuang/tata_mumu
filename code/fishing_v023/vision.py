"""Pure image recognition. No OCR, network, input hooks, or game-memory access."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import json
import cv2
import numpy as np

WIDTH, HEIGHT = 588, 968

@dataclass
class Observation:
    state: str
    x: float | None = None
    target: tuple[float, float] | None = None
    flash: bool = False
    can_collect: bool = False
    scores: dict = field(default_factory=dict)
    control: tuple[float, float] | None = None
    bite_fraction: float | None = None

class Vision:
    def __init__(self, assets: Path):
        self.templates = []
        for item in json.loads((assets / 'templates.json').read_text('utf-8')):
            im = cv2.imdecode(np.fromfile(str(assets / item['file']), dtype=np.uint8), cv2.IMREAD_COLOR)
            if im is None:
                raise RuntimeError(f"Missing template: {item['file']}")
            self.templates.append((item, im))
        self.soft_templates = {}
        for name in ('congrats', 'weight_kg', 'record', 'bite'):
            im = cv2.imdecode(np.fromfile(str(assets / (name+'.png')), np.uint8), 0)
            if im is None:
                raise RuntimeError('Missing template: '+name)
            self.soft_templates[name] = cv2.GaussianBlur(
                cv2.resize(im, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA), (3,3), 0)
        self.bounds = None
        self.bounds_time = -999.0

    def reset(self):
        self.bounds = None
        self.bounds_time = -999.0

    def template_scores(self, im):
        scores = {}
        for item, templ in self.templates:
            x, y, w, h = item['rect']
            pad = item.get('pad', 8)
            sx,sy=max(0,x-pad),max(0,y-pad)
            search = im[sy:min(HEIGHT,y+h+pad), sx:min(WIDTH,x+w+pad)]
            result = cv2.matchTemplate(search, templ, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(result)
            patch = search[loc[1]:loc[1]+h, loc[0]:loc[0]+w]
            delta = np.mean(cv2.absdiff(patch, templ))
            scores[item['name']] = round(float(score if delta < 48 else 0.0), 3)
            scores[item['name']+'_dy']=sy+loc[1]-y
            scores[item['name']+'_dx']=sx+loc[0]-x
        return scores

    def soft_score(self, name, frame, rect):
        """Low-pass glyph comparison tolerates resized/antialiased UI, not arbitrary dialogs."""
        x,y,w,h = rect
        search = cv2.cvtColor(frame[y:y+h,x:x+w], cv2.COLOR_BGR2GRAY)
        search = cv2.GaussianBlur(cv2.resize(search,None,fx=.5,fy=.5,
                                interpolation=cv2.INTER_AREA),(3,3),0)
        templ = self.soft_templates[name]
        if min(search.shape) < 1 or any(a<b for a,b in zip(search.shape,templ.shape)):
            return 0., (x,y)
        _,score,_,pos=cv2.minMaxLoc(cv2.matchTemplate(search,templ,cv2.TM_CCOEFF_NORMED))
        return round(float(score),3),(x+2*pos[0],y+2*pos[1])

    def result_card(self, frame, hsv, scores):
        """Recognize a result by layout, never by fish/name/record headline.

        The blue/orange card, dark header/footer and kg badge are independent checks.
        A weaker but still result-shaped frame permits a bounded side-panel tap.
        Completely unrecognized dialogs do not become clickable.
        """
        blue = (cv2.inRange(hsv, (85, 75, 85), (122, 255, 255)) |
                 cv2.inRange(hsv, (10, 75, 85), (40, 255, 255))) > 0
        support = np.minimum(blue[:, 20:125].mean(1), blue[:, 460:568].mean(1))
        # Close small confetti holes in the row mask, not entire missing panels.
        rows = (support > .56).astype(np.uint8)
        rows[:225] = 0; rows[830:] = 0
        rows = cv2.morphologyEx(rows[:, None], cv2.MORPH_CLOSE,
                               np.ones((23, 1), np.uint8))[:, 0]
        ys = np.flatnonzero(rows)
        groups = np.split(ys, np.where(np.diff(ys) > 1)[0]+1)
        groups = [g for g in groups if len(g) >= 170]
        if not groups:
            scores.update(card_blue=0., card_footer=0.)
            return None
        band = max(groups, key=len)
        top, bottom = int(band[0]), int(band[-1])+1
        sides = float(np.mean(support[top+10:bottom-10]))
        footer = float((hsv[bottom+18:min(HEIGHT,bottom+58), 30:558, 2] < 90).mean())
        header = float((hsv[max(0,top-105):top-35, 30:558, 2] < 145).mean())
        scores.update(card_blue=round(sides,3), card_footer=round(footer,3),
                      card_header=round(header,3), card_top=top, card_bottom=bottom)
        if sides < .55 or footer < .43 or header < .43 or bottom < 555:
            return None
        kg_y = max(top+80, bottom-175)
        kg, pos = self.soft_score('weight_kg', frame,
                                  (180, kg_y, 335, min(210,HEIGHT-kg_y)))
        scores['card_kg'] = kg
        # The unit must be on the lower part of this card, not a footer dialog.
        unit_in_band = bottom-165 <= pos[1] <= bottom-10
        if not unit_in_band or kg < .57:
            return None
        scores['card_layout'] = 1.
        # The headline is optional metadata only. All headlines can be cleared
        # and the same result remains collectable, including personal records.
        if kg >= .72 and sides >= .64 and footer >= .50:
            kind = 'RECORD' if scores.get('record',0) > .78 else 'RESULT'
            return Observation(kind, can_collect=True, scores=scores,
                               control=(80, min(820,bottom+65)))
        # If recognition is imperfect, use a blank side of the card. Verify
        # blue background at that exact landing point rather than hitting OK.
        py = min(bottom-90, max(top+80, 520))
        clean = float(blue[py-14:py+15, 65:96].mean())
        scores['continue_patch'] = round(clean,3)
        if clean >= .90:
            scores['safe_continue'] = True
            return Observation('CONTINUE', can_collect=True, scores=scores,
                               control=(80,py))
        return None

    def bite_observation(self, frame, hsv, scores, purple):
        if purple<=.15:
            return None
        # Text can sit outside v020's +/-10 pixel band during layout/animation.
        dy=int(scores.get('bite_dy',0));dx=int(scores.get('bite_dx',0))
        text=scores.get('bite',0)
        if text<=.75:
            text,(tx,ty)=self.soft_score('bite',frame,(161,595,267,108))
            scores['bite_soft']=text
            dx,dy=tx-204,ty-635
            if text<.79:return None
        panel=hsv[626+dy:669+dy,151+dx:442+dx]
        panel_purple=float((cv2.inRange(panel,(118,85,105),(170,255,255))>0).mean())
        scores['bite_panel']=round(panel_purple,3)
        if panel_purple<.28:return None
        strip=hsv[679+dy:692+dy,155+dx:438+dx]
        yellow=cv2.inRange(strip,(19,95,150),(39,255,255))
        xs=np.where(np.sum(yellow>0,axis=0)>=3)[0]
        fraction=float((xs[-1]+1)/strip.shape[1]) if len(xs) else 0.
        scores['bite_fraction']=round(fraction,3)
        scores['bite_source']='text+panel+button'
        self.reset()
        return Observation('BITE',scores=scores,control=(295,858),bite_fraction=fraction)

    def analyze(self, frame: np.ndarray, now: float) -> Observation:
        if frame.shape[:2] != (HEIGHT, WIDTH):
            frame = cv2.resize(frame, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR)
        scores = self.template_scores(frame)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        # Card structure comes first: both result layouts say "congratulations".
        card=self.result_card(frame,hsv,scores)
        if card is not None:return card
        # Reward layouts take priority, even if a dim QTE bar remains underneath.
        if scores['loot'] > .79:
            region = hsv[890:952,175:410]
            green = cv2.inRange(region, (33,100,120), (83,255,255))
            return Observation('REWARD', can_collect=float((green>0).mean()) > .40,
                               scores=scores)
        if scores['record'] > .78:
            return Observation('RECORD', can_collect=True, scores=scores)
        if scores['title'] < .75:
            return Observation('UNKNOWN', scores=scores)
        button = hsv[802:916,234:355]
        green = float((cv2.inRange(button,(33,100,105),(90,255,255))>0).mean())
        purple = float((cv2.inRange(button,(118,85,105),(170,255,255))>0).mean())
        gray = float(((button[:,:,1]<65)&(button[:,:,2]>60)).mean())
        scores.update(green=round(green,3),purple=round(purple,3),gray=round(gray,3))
        bite=self.bite_observation(frame,hsv,scores,purple)
        if bite is not None:return bite
        # The QTE bottom outline is below the similarly colored bite progress bar.
        border = scores['bar']
        scores['border'] = round(border,3)
        if border > .73 and purple > .15:
            dy=int(scores.get('bar_dy',0))
            dx=int(scores.get('bar_dx',0))
            strip = hsv[694+dy:713+dy,160+dx:445+dx]
            brightness = float(((strip[:,:,1]<105)&(strip[:,:,2]>220)).mean())
            flash = brightness > .20
            scores['flash_fraction'] = round(brightness,3)
            if flash:
                self.reset()
                return Observation('QTE', flash=True, scores=scores)
            region = hsv[660+dy:734+dy,150+dx:456+dx]
            red = (cv2.inRange(region,(0,110,95),(13,255,255)) |
                   cv2.inRange(region,(168,110,95),(179,255,255)))
            contours,_ = cv2.findContours(red,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            candidates=[]
            for contour in contours:
                x,y,w,h = cv2.boundingRect(contour)
                area=cv2.contourArea(contour)
                if 7<=w<=34 and 12<=h<=65 and area>70 and y<42:
                    candidates.append((area,x+w/2+150+dx))
            marker=max(candidates)[1] if candidates else None
            yellow = cv2.inRange(hsv[694+dy:713+dy,145+dx:454+dx],(19,95,170),(39,255,255))
            xs = np.where(np.sum(yellow>0,axis=0)>=4)[0]+145+dx
            target=None
            if len(xs)>=5:
                # Merge the two pieces of a yellow target occluded by the marker.
                groups=np.split(xs,np.where(np.diff(xs)>1)[0]+1)
                groups=[g for g in groups if len(g)>=3]
                if groups:
                    best=max(groups,key=len)
                    joined=[best]
                    for g in groups:
                        if g is best: continue
                        gap=max(g[0]-best[-1],best[0]-g[-1])
                        if (marker is not None and gap<=40 and
                                min(best[0],g[0])<marker<max(best[-1],g[-1])):
                            joined.append(g)
                    l=float(min(g[0] for g in joined)); r=float(max(g[-1] for g in joined))
                    if self.bounds and now-self.bounds_time < .22 and marker is not None:
                        oldl,oldr=self.bounds
                        if abs(marker-oldl)<24 and l>oldl and l<marker+25:
                            l=oldl
                        if abs(marker-oldr)<24 and r<oldr and r>marker-25:
                            r=oldr
                    if 7<=r-l<=230:
                        target=(l,r)
                        # Cache only a clear view of both boundaries.
                        if marker is None or (abs(marker-l)>23 and abs(marker-r)>23):
                            self.bounds=target; self.bounds_time=now
            elif self.bounds and now-self.bounds_time < .18 and marker is not None:
                l,r=self.bounds
                if l-20<marker<r+20:
                    target=self.bounds
            return Observation('QTE',x=marker,target=target,scores=scores)
        if green > .26:
            self.reset()
            return Observation('READY',scores=scores,control=(295,858))
        if gray > .34:
            return Observation('WAIT',scores=scores)
        return Observation('TRANSITION',scores=scores)


def annotate(frame: np.ndarray, obs: Observation, predicted=None) -> np.ndarray:
    out=cv2.resize(frame,(WIDTH,HEIGHT)).copy()
    cv2.rectangle(out,(0,74),(WIDTH,123),(20,20,20),-1)
    text=obs.state + (' / FLASH: input blocked' if obs.flash else '')
    cv2.putText(out,text,(12,103),cv2.FONT_HERSHEY_SIMPLEX,.64,(255,255,255),2)
    dy=int(obs.scores.get('bar_dy',0))
    if obs.target:
        l,r=map(int,obs.target)
        cv2.rectangle(out,(l,687+dy),(r,717+dy),(0,210,0),2)
    if obs.x is not None:
        cv2.line(out,(int(obs.x),659+dy),(int(obs.x),735+dy),(0,0,255),2)
    if predicted is not None:
        cv2.line(out,(int(predicted),681+dy),(int(predicted),731+dy),(255,0,255),2)
    return out
