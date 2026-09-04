// The first screen: state your taste, and the kernel starts somewhere real.
//
// Without it a new visitor meets an empty recommender — `kernelRecommend` returns
// nothing until t > 0, so "Where you're heading" is simply absent until they have
// played something. Seeding from stated preferences means the demo opens with the
// rail already populated, which is the thing worth showing.
//
// The picks are applied as ordinary `onPlay` transitions in the worker (see
// Graph.kernelSeed), so this screen teaches the kernel nothing it couldn't have
// learned from listening.
import { useEffect, useState } from 'react';
import { onboardingOptions, type OnboardingOptions } from '../lib/client';
import { useKernel } from '../lib/kernel';

/** Enough genres to place μ somewhere specific rather than in the middle of everything. */
const MIN_GENRES = 3;
const N_ARTISTS = 3;

export default function Onboarding() {
  const { seed } = useKernel();
  const [opts, setOpts] = useState<OnboardingOptions | null>(null);
  const [genres, setGenres] = useState<Set<string>>(new Set());
  const [artists, setArtists] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => { void onboardingOptions().then(setOpts); }, []);

  const toggle = (
    set: Set<string>, put: (s: Set<string>) => void, id: string, cap?: number,
  ) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else if (!cap || next.size < cap) next.add(id);
    put(next);
  };

  const ready = genres.size >= MIN_GENRES && artists.size === N_ARTISTS;

  if (!opts) return null;

  return (
    <section className="onboard">
      <div className="hero-kicker">First, tell it what you like</div>
      <h1>Seed the <em>kernel</em>.</h1>
      <p className="onboard-lede">
        Everything below is read out of the catalogue — no profile, no account,
        nothing sent anywhere. Your picks become the starting point for a
        recommender that runs entirely in this browser.
      </p>

      <div className="onboard-step">
        <h2>
          Pick your genres
          <span className="count">
            {genres.size < MIN_GENRES
              ? `choose at least ${MIN_GENRES} — ${genres.size} so far`
              : `${genres.size} chosen`}
          </span>
        </h2>
        <div className="onboard-chips">
          {opts.genres.map((g) => (
            <button
              key={g.id}
              className={`onboard-chip ${genres.has(g.id) ? 'is-on' : ''}`}
              aria-pressed={genres.has(g.id)}
              onClick={() => toggle(genres, setGenres, g.id)}
            >
              {g.title}
              <span className="onboard-chip-n">{g.tracks.toLocaleString()}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="onboard-step">
        <h2>
          Follow {N_ARTISTS} artists
          <span className="count">{artists.size} of {N_ARTISTS} chosen</span>
        </h2>
        <div className="onboard-artists">
          {opts.artists.map((a) => {
            const on = artists.has(a.id);
            const full = artists.size >= N_ARTISTS && !on;
            return (
              <button
                key={a.id}
                className={`onboard-artist ${on ? 'is-on' : ''}`}
                aria-pressed={on}
                disabled={full}
                onClick={() => toggle(artists, setArtists, a.id, N_ARTISTS)}
              >
                <span className="onboard-artist-name">{a.name}</span>
                <span className="onboard-artist-meta">
                  {a.followers.toLocaleString()} followers · {a.tracks} tracks
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <button
        className="onboard-go"
        disabled={!ready || busy}
        onClick={() => { setBusy(true); void seed([...genres], [...artists]); }}
      >
        {busy ? 'Seeding…' : 'Start listening'}
      </button>
      {!ready && (
        <div className="onboard-hint">
          {genres.size < MIN_GENRES && `${MIN_GENRES - genres.size} more genre${MIN_GENRES - genres.size === 1 ? '' : 's'}`}
          {genres.size < MIN_GENRES && artists.size !== N_ARTISTS && ' · '}
          {artists.size !== N_ARTISTS && `${N_ARTISTS - artists.size} more artist${N_ARTISTS - artists.size === 1 ? '' : 's'}`}
        </div>
      )}
    </section>
  );
}
