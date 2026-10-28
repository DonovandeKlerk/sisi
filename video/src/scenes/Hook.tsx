import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {FONT, PALETTE} from '../timing';

const HEADLINE = 'Over 100,000 spaza shops run on memory and paper.'.split(' ');
const PAINS = ['The ibhuku, in pencil', 'Stock counted by eye', 'Expiry dates nobody checks'];

export const Hook: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return (
    <div
      style={{
        flex: 1,
        backgroundColor: PALETTE.bg,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        padding: '0 160px',
        fontFamily: FONT,
      }}
    >
      <div
        style={{
          fontSize: 30,
          letterSpacing: 8,
          fontWeight: 700,
          color: PALETTE.yellow,
          opacity: interpolate(frame, [0, 20], [0, 1], {extrapolateRight: 'clamp'}),
        }}
      >
        AMANZIMTOTI · KWAZULU-NATAL
      </div>
      <h1
        style={{
          fontSize: 104,
          lineHeight: 1.12,
          fontWeight: 800,
          color: PALETTE.ink,
          margin: '28px 0 0',
          maxWidth: 1500,
        }}
      >
        {HEADLINE.map((word, i) => {
          const s = spring({frame: frame - 10 - i * 3, fps, config: {damping: 200}});
          return (
            <span
              key={i}
              style={{
                display: 'inline-block',
                opacity: s,
                transform: `translateY(${interpolate(s, [0, 1], [60, 0])}px)`,
                marginRight: 26,
                color: word.includes('100,000') ? PALETTE.bubble : PALETTE.ink,
              }}
            >
              {word}
            </span>
          );
        })}
      </h1>
      <div style={{display: 'flex', gap: 28, marginTop: 64}}>
        {PAINS.map((pain, i) => {
          const s = spring({frame: frame - 90 - i * 12, fps});
          return (
            <div
              key={pain}
              style={{
                opacity: s,
                transform: `translateY(${interpolate(s, [0, 1], [40, 0])}px)`,
                backgroundColor: PALETTE.card,
                borderRadius: 20,
                padding: '26px 34px',
                fontSize: 38,
                fontWeight: 600,
                color: PALETTE.ink,
                borderLeft: `8px solid ${[PALETTE.yellow, PALETTE.red, PALETTE.blue][i]}`,
              }}
            >
              {pain}
            </div>
          );
        })}
      </div>
      <div
        style={{
          marginTop: 44,
          fontSize: 26,
          color: PALETTE.muted,
          opacity: interpolate(frame, [200, 230], [0, 1], {extrapolateRight: 'clamp'}),
        }}
      >
        ±150,000 spazas in SA — UCT / Accenture Africa 2023
      </div>
    </div>
  );
};
