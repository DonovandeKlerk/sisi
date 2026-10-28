import React from 'react';

// Static film grain (deterministic SVG turbulence, no animation).
export const Grain: React.FC = () => (
  <svg
    width="1920"
    height="1080"
    style={{position: 'absolute', inset: 0, opacity: 0.055, pointerEvents: 'none'}}
  >
    <filter id="sisi-grain">
      <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" />
      <feColorMatrix type="saturate" values="0" />
    </filter>
    <rect width="1920" height="1080" filter="url(#sisi-grain)" />
  </svg>
);
