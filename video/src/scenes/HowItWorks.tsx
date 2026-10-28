import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {FONT, PALETTE} from '../timing';

const NODES = [
  {x: 170, label: 'WhatsApp', sub: 'text · voice · photo', color: PALETTE.bubble},
  {x: 610, label: 'Webhook on Modal', sub: 'FastAPI · always on', color: PALETTE.yellow},
  {x: 1050, label: 'muse-spark-1.3', sub: 'agent + vision · json_schema', color: '#fff'},
  {x: 1490, label: 'Shop database', sub: 'SQLite · per-shop', color: PALETTE.bubble},
];

const VoiceNode = {x: 1050, y: 700, label: 'muse-voice-transcribe-1.0', sub: 'voice notes'};

const Pulse: React.FC<{x1: number; x2: number; y: number; delay: number}> = ({x1, x2, y, delay}) => {
  const frame = useCurrentFrame();
  const p = ((frame - delay) % 70) / 70;
  if (frame < delay) {
    return null;
  }
  return (
    <circle cx={x1 + (x2 - x1) * p} cy={y} r={10} fill={PALETTE.yellow} opacity={1 - p * 0.6} />
  );
};

export const HowItWorks: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const privacy = spring({frame: frame - 600, fps});
  return (
    <div
      style={{
        flex: 1,
        backgroundColor: PALETTE.bg,
        fontFamily: FONT,
        padding: '90px 120px 0',
      }}
    >
      <div style={{fontSize: 30, letterSpacing: 8, fontWeight: 700, color: PALETTE.yellow}}>
        HOW IT WORKS
      </div>
      <svg width={1680} height={560} style={{marginTop: 20}}>
        {NODES.slice(0, 3).map((n, i) => (
          <line
            key={i}
            x1={n.x + 130}
            y1={200}
            x2={NODES[i + 1].x - 130}
            y2={200}
            stroke={PALETTE.muted}
            strokeWidth={4}
            opacity={0.5}
          />
        ))}
        <line x1={1050} y1={330} x2={1050} y2={420} stroke={PALETTE.muted} strokeWidth={4} opacity={0.5} />
        <Pulse x1={300} x2={480} y={200} delay={150} />
        <Pulse x1={740} x2={920} y={200} delay={220} />
        <Pulse x1={1180} x2={1360} y={200} delay={290} />
        {NODES.map((n, i) => {
          const s = spring({frame: frame - 40 - i * 55, fps});
          return (
            <g key={n.label} opacity={s}>
              <rect x={n.x - 130} y={100} width={260} height={200} rx={24} fill={PALETTE.card} />
              <rect x={n.x - 130} y={100} width={260} height={14} rx={7} fill={n.color} />
              <text x={n.x} y={205} textAnchor="middle" fill="#fff" fontSize={i === 2 ? 33 : 40} fontWeight={700}>
                {n.label}
              </text>
              <text x={n.x} y={250} textAnchor="middle" fill={PALETTE.muted} fontSize={26}>
                {n.sub}
              </text>
            </g>
          );
        })}
        {(() => {
          const s = spring({frame: frame - 320, fps});
          return (
            <g opacity={s}>
              <rect x={VoiceNode.x - 220} y={420} width={440} height={110} rx={24} fill={PALETTE.card} />
              <text x={VoiceNode.x} y={470} textAnchor="middle" fill="#fff" fontSize={30} fontWeight={700}>
                {VoiceNode.label}
              </text>
              <text x={VoiceNode.x} y={505} textAnchor="middle" fill={PALETTE.muted} fontSize={24}>
                {VoiceNode.sub}
              </text>
            </g>
          );
        })()}
      </svg>
      <div
        style={{
          opacity: privacy,
          display: 'flex',
          gap: 24,
          marginTop: 10,
        }}
      >
        {['Owner confirms every number', 'Undo with one word', "POPIA: 'delete my data' erases all"].map(
          (t) => (
            <div
              key={t}
              style={{
                backgroundColor: 'rgba(181,216,137,0.14)',
                border: `2px solid ${PALETTE.bubble}`,
                borderRadius: 18,
                padding: '18px 30px',
                fontSize: 32,
                fontWeight: 600,
                color: PALETTE.ink,
              }}
            >
              {t}
            </div>
          ),
        )}
      </div>
    </div>
  );
};
