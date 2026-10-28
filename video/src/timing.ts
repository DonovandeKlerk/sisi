// Shared timing, palette, demo slots, and burned-in captions.
// Film: 172s (2:52) at 30fps, 1920x1080.

export const FPS = 30;
export const DURATION_SECONDS = 172;
export const DURATION_FRAMES = DURATION_SECONDS * FPS;

export const PALETTE = {
  bg: '#062A1E',
  card: '#01553A',
  bubble: '#B5D789',
  yellow: '#F4B30A',
  red: '#E73225',
  blue: '#0E6DE5',
  ink: '#F9FBFA',
  muted: '#9DB8A8',
} as const;

export const FONT =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";

export const SCENES = {
  hook: {start: 0, end: 15},
  meet: {start: 15, end: 35},
  demo: {start: 35, end: 120},
  how: {start: 120, end: 150},
  impact: {start: 150, end: 172},
} as const;

export type DemoClip = {
  file: string;
  start: number;
  end: number;
  kicker: string;
  title: string;
  caption: string;
};

export const DEMO_CLIPS: DemoClip[] = [
  {
    file: 'rec1-credit.mp4',
    start: 35,
    end: 49,
    kicker: 'CREDIT BOOK',
    title: '“How much does Sipho owe?”',
    caption: 'The ibhuku, answered in seconds. R73.00, every cent accounted for.',
  },
  {
    file: 'rec2-sale.mp4',
    start: 49,
    end: 63,
    kicker: 'SALE + STOCK',
    title: 'One message sells. Stock drops itself.',
    caption: 'Low-stock alerts before the customer notices.',
  },
  {
    file: 'rec3-voice.mp4',
    start: 63,
    end: 77,
    kicker: 'VOICE NOTE',
    title: 'Her own words become the book entry.',
    caption: 'Voice-first, in English or isiZulu. Always confirmed first.',
  },
  {
    file: 'rec4-expiry.mp4',
    start: 77,
    end: 91,
    kicker: 'EXPIRY PHOTO',
    title: 'Every date stamp, read.',
    caption: 'Expired milk off the shelf before it can harm anyone.',
  },
  {
    file: 'rec5-cashup.mp4',
    start: 91,
    end: 104,
    kicker: 'CASH-UP',
    title: '“Cash up.” Done.',
    caption: 'Cash, book sales and payments in, totalled in seconds.',
  },
  {
    file: 'rec6-compliance.mp4',
    start: 104,
    end: 120,
    kicker: 'COMPLIANCE',
    title: 'Permits, the fund — and the scam warning.',
    caption: 'Official links only. Applications are free.',
  },
];

export type CaptionLine = {start: number; end: number; text: string};

export const CAPTIONS: CaptionLine[] = [
  {start: 0, end: 6, text: 'Under the counter of almost every spaza shop in South Africa,'},
  {start: 6, end: 10, text: 'there is a school exercise book. The ibhuku.'},
  {start: 10, end: 15, text: 'Who owes what, in pencil. Stock by eye. Dates nobody checks.'},
  {start: 15, end: 20, text: 'Meet Sisi. A WhatsApp assistant for spaza owners.'},
  {start: 20, end: 24, text: 'No app to install, nothing new to learn.'},
  {start: 24, end: 29, text: 'She speaks English and isiZulu, by text or voice note.'},
  {start: 29, end: 35, text: 'And she never makes a mess of your records.'},
  {start: 35, end: 40, text: 'MaZulu asks: how much does Sipho owe?'},
  {start: 40, end: 49, text: 'Sisi checks the credit book. Seventy-three rand.'},
  {start: 49, end: 55, text: 'A sale is one message. Stock drops by itself.'},
  {start: 55, end: 63, text: 'When the shelf runs low, Sisi says so first.'},
  {start: 63, end: 70, text: 'No typing needed. A voice note in her own words'},
  {start: 70, end: 77, text: 'becomes a neat book entry — always confirmed first.'},
  {start: 77, end: 84, text: 'A photo of the fridge, and Sisi reads every date stamp.'},
  {start: 84, end: 91, text: 'Expired milk, off the shelf, before it harms anyone.'},
  {start: 91, end: 96, text: 'At seven, one word: cash up.'},
  {start: 96, end: 104, text: 'Cash, book sales and payments in, totalled in seconds.'},
  {start: 104, end: 111, text: 'And the paperwork: permits, the spaza fund,'},
  {start: 111, end: 120, text: 'and a warning that applications are free.'},
  {start: 120, end: 126, text: "Behind Sisi: Meta's Muse Spark reads and reasons."},
  {start: 126, end: 132, text: 'Muse Voice Transcribe hears every voice note.'},
  {start: 132, end: 139, text: 'And vision checks every shelf, dates in strict structured output.'},
  {start: 139, end: 145, text: 'Every number confirmed by the owner — undone with one word.'},
  {start: 145, end: 150, text: "Your records stay yours. Say delete my data, and they're gone."},
  {start: 150, end: 156, text: 'One hundred and fifty thousand spaza shops feed the townships.'},
  {start: 156, end: 161, text: 'Five percent of GDP. Millions of jobs.'},
  {start: 161, end: 166, text: 'Sisi gives each owner a sister who never forgets.'},
  {start: 166, end: 172, text: "Built with Meta's models. Meet Sisi."},
];
