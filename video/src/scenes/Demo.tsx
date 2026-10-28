import React from 'react';
import {Sequence, interpolate, useCurrentFrame} from 'remotion';
import {DEMO_CLIPS, FONT, FPS, PALETTE, SCENES} from '../timing';
import {PhoneMockup} from '../components/PhoneMockup';

const DemoClip: React.FC<{index: number}> = ({index}) => {
  const frame = useCurrentFrame();
  const clip = DEMO_CLIPS[index];
  const enter = interpolate(frame, [0, 24], [80, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const fade = interpolate(frame, [0, 18], [0, 1], {extrapolateRight: 'clamp'});
  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 130,
        fontFamily: FONT,
        opacity: fade,
        transform: `translateX(${enter}px)`,
      }}
    >
      <div style={{maxWidth: 760}}>
        <div style={{fontSize: 30, letterSpacing: 8, fontWeight: 700, color: PALETTE.yellow}}>
          {clip.kicker} · {index + 1}/6
        </div>
        <div style={{fontSize: 76, fontWeight: 800, color: PALETTE.ink, marginTop: 22, lineHeight: 1.15}}>
          {clip.title}
        </div>
        <div style={{fontSize: 40, color: PALETTE.bubble, marginTop: 26, lineHeight: 1.4}}>
          {clip.caption}
        </div>
      </div>
      <PhoneMockup file={clip.file} label="LIVE RECORDING" />
    </div>
  );
};

export const Demo: React.FC = () => (
  <div style={{flex: 1, backgroundColor: PALETTE.bg, display: 'flex'}}>
    {DEMO_CLIPS.map((clip, i) => (
      <Sequence
        key={clip.file}
        from={(clip.start - SCENES.demo.start) * FPS}
        durationInFrames={(clip.end - clip.start) * FPS}
        name={clip.kicker}
      >
        <DemoClip index={i} />
      </Sequence>
    ))}
  </div>
);
