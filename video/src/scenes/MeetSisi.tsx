import React from 'react';
import {Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {FONT, PALETTE} from '../timing';

const PILLS = ['WhatsApp · no app to install', 'English + isiZulu', 'Text + voice notes'];

export const MeetSisi: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const logoScale = spring({frame, fps, config: {damping: 14}});
  const ownerMsg = spring({frame: frame - 200, fps});
  const sisiMsg = spring({frame: frame - 280, fps});
  return (
    <div
      style={{
        flex: 1,
        backgroundColor: PALETTE.bg,
        display: 'flex',
        alignItems: 'center',
        padding: '0 160px',
        gap: 110,
        fontFamily: FONT,
      }}
    >
      <Img
        src={staticFile('sisi-logo.png')}
        style={{
          width: 460,
          height: 460,
          borderRadius: 110,
          transform: `scale(${logoScale})`,
          boxShadow: '0 30px 100px rgba(0,0,0,0.5)',
        }}
      />
      <div>
        <div
          style={{
            fontSize: 110,
            fontWeight: 800,
            color: PALETTE.ink,
            opacity: interpolate(frame, [20, 45], [0, 1], {extrapolateRight: 'clamp'}),
          }}
        >
          Meet Sisi.
        </div>
        <div
          style={{
            fontSize: 44,
            color: PALETTE.bubble,
            fontWeight: 600,
            marginTop: 10,
            opacity: interpolate(frame, [45, 70], [0, 1], {extrapolateRight: 'clamp'}),
          }}
        >
          A WhatsApp assistant for spaza owners.
        </div>
        <div style={{display: 'flex', gap: 20, marginTop: 40}}>
          {PILLS.map((pill, i) => {
            const s = spring({frame: frame - 90 - i * 14, fps});
            return (
              <div
                key={pill}
                style={{
                  opacity: s,
                  backgroundColor: 'rgba(181,216,137,0.14)',
                  border: `2px solid ${PALETTE.bubble}`,
                  color: PALETTE.ink,
                  borderRadius: 999,
                  padding: '16px 30px',
                  fontSize: 32,
                  fontWeight: 600,
                }}
              >
                {pill}
              </div>
            );
          })}
        </div>
        <div style={{marginTop: 44, display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 760}}>
          <div
            style={{
              opacity: ownerMsg,
              alignSelf: 'flex-end',
              backgroundColor: '#0E6DE5',
              color: '#fff',
              borderRadius: '26px 26px 8px 26px',
              padding: '18px 28px',
              fontSize: 34,
            }}
          >
            Sipho took two bread and a 2L Coke 🎙️
          </div>
          <div
            style={{
              opacity: sisiMsg,
              alignSelf: 'flex-start',
              backgroundColor: PALETTE.card,
              color: PALETTE.ink,
              borderRadius: '26px 26px 26px 8px',
              padding: '18px 28px',
              fontSize: 34,
            }}
          >
            Saved: Sipho +R73.00. Say Yes to confirm. ✅
          </div>
        </div>
      </div>
    </div>
  );
};
