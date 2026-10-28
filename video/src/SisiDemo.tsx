import React from 'react';
import {AbsoluteFill, Audio, Sequence, staticFile} from 'remotion';
import {DURATION_FRAMES, FPS, PALETTE, SCENES} from './timing';
import {Captions} from './components/Captions';
import {Grain} from './components/Grain';
import {Demo} from './scenes/Demo';
import {Hook} from './scenes/Hook';
import {HowItWorks} from './scenes/HowItWorks';
import {Impact} from './scenes/Impact';
import {MeetSisi} from './scenes/MeetSisi';

const seq = (s: {start: number; end: number}) => ({
  from: s.start * FPS,
  durationInFrames: (s.end - s.start) * FPS,
});

export const SisiDemo: React.FC = () => (
  <AbsoluteFill style={{backgroundColor: PALETTE.bg}}>
    <Sequence {...seq(SCENES.hook)} name="Hook">
      <Hook />
    </Sequence>
    <Sequence {...seq(SCENES.meet)} name="Meet Sisi">
      <MeetSisi />
    </Sequence>
    <Sequence {...seq(SCENES.demo)} name="Live demo">
      <Demo />
    </Sequence>
    <Sequence {...seq(SCENES.how)} name="How it works">
      <HowItWorks />
    </Sequence>
    <Sequence {...seq(SCENES.impact)} name="Impact">
      <Impact />
    </Sequence>
    <Audio src={staticFile('voiceover.mp3')} />
    <Grain />
    <Captions />
  </AbsoluteFill>
);

export const DURATION = DURATION_FRAMES;
