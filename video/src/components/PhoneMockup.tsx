import React from 'react';
import {Video, staticFile} from 'remotion';
import {FONT, PALETTE} from '../timing';

export const PhoneMockup: React.FC<{file: string; label: string}> = ({file, label}) => (
  <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
    <div
      style={{
        width: 400,
        height: 780,
        borderRadius: 56,
        backgroundColor: '#0A0A0A',
        padding: 16,
        boxShadow: '0 40px 120px rgba(0,0,0,0.55)',
        border: '2px solid rgba(255,255,255,0.14)',
        position: 'relative',
      }}
    >
      <div
        style={{
          position: 'absolute',
          top: 30,
          left: '50%',
          transform: 'translateX(-50%)',
          width: 130,
          height: 26,
          borderRadius: 13,
          backgroundColor: '#0A0A0A',
          zIndex: 2,
        }}
      />
      <div style={{width: '100%', height: '100%', borderRadius: 42, overflow: 'hidden'}}>
        <Video
          src={staticFile(`recordings/${file}`)}
          style={{width: '100%', height: '100%', objectFit: 'cover'}}
        />
      </div>
    </div>
    <div
      style={{
        marginTop: 22,
        fontFamily: FONT,
        fontSize: 26,
        letterSpacing: 4,
        fontWeight: 700,
        color: PALETTE.bubble,
      }}
    >
      {label}
    </div>
  </div>
);
