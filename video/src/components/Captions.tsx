import React from 'react';
import {interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {CAPTIONS, FONT} from '../timing';

export const Captions: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t = frame / fps;
  const line = CAPTIONS.find((c) => t >= c.start && t < c.end);
  if (!line) {
    return null;
  }
  const opacity = interpolate(
    t,
    [line.start, line.start + 0.25, line.end - 0.25, line.end],
    [0, 1, 1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        bottom: 56,
        display: 'flex',
        justifyContent: 'center',
        opacity,
        pointerEvents: 'none',
      }}
    >
      <div
        style={{
          fontFamily: FONT,
          fontSize: 40,
          fontWeight: 600,
          color: '#fff',
          backgroundColor: 'rgba(0,0,0,0.72)',
          padding: '14px 34px',
          borderRadius: 16,
          maxWidth: 1500,
          textAlign: 'center',
          lineHeight: 1.35,
        }}
      >
        {line.text}
      </div>
    </div>
  );
};
