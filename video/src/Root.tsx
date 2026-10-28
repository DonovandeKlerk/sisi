import React from 'react';
import {Composition} from 'remotion';
import {SisiDemo} from './SisiDemo';
import {DURATION_FRAMES, FPS} from './timing';

export const Root: React.FC = () => (
  <Composition
    id="SisiDemo"
    component={SisiDemo}
    durationInFrames={DURATION_FRAMES}
    fps={FPS}
    width={1920}
    height={1080}
  />
);
