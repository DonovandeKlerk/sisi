import React from 'react';
import {Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {FONT, PALETTE} from '../timing';

const STATS = [
  {big: '150,000', small: 'spaza shops (UCT / Accenture 2023)'},
  {big: '5.2%', small: 'of GDP (UCT)'},
  {big: '2.6M', small: 'jobs (UCT)'},
];

export const Impact: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return (
    <div
      style={{
        flex: 1,
        backgroundColor: PALETTE.bg,
        fontFamily: FONT,
        display: 'flex',
        alignItems: 'center',
        padding: '0 160px',
        gap: 120,
      }}
    >
      <div style={{flex: 1}}>
        <div style={{fontSize: 30, letterSpacing: 8, fontWeight: 700, color: PALETTE.yellow}}>
          IMPACT
        </div>
        <div style={{fontSize: 72, fontWeight: 800, color: PALETTE.ink, marginTop: 18, lineHeight: 1.15}}>
          A sister who never forgets, for every spaza.
        </div>
        <div style={{display: 'flex', gap: 60, marginTop: 44}}>
          {STATS.map((s, i) => {
            const p = spring({frame: frame - 60 - i * 25, fps});
            return (
              <div key={s.big} style={{opacity: p}}>
                <div style={{fontSize: 84, fontWeight: 800, color: PALETTE.bubble}}>{s.big}</div>
                <div style={{fontSize: 28, color: PALETTE.muted, marginTop: 6}}>{s.small}</div>
              </div>
            );
          })}
        </div>
        <div
          style={{
            marginTop: 44,
            fontSize: 34,
            color: PALETTE.ink,
            opacity: interpolate(frame, [220, 250], [0, 1], {extrapolateRight: 'clamp'}),
          }}
        >
          Next: more languages, shelf counts, more shops.
        </div>
        <div
          style={{
            marginTop: 16,
            fontSize: 34,
            fontWeight: 700,
            color: PALETTE.yellow,
            opacity: interpolate(frame, [260, 290], [0, 1], {extrapolateRight: 'clamp'}),
          }}
        >
          github.com/donovandeklerk/sisi
        </div>
      </div>
      <Img
        src={staticFile('sisi-logo.png')}
        style={{
          width: 380,
          height: 380,
          borderRadius: 90,
          transform: `scale(${spring({frame: frame - 300, fps, config: {damping: 14}})})`,
          boxShadow: '0 30px 100px rgba(0,0,0,0.5)',
        }}
      />
    </div>
  );
};
