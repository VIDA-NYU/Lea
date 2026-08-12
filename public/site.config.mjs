/**
 * Single source of truth for everything the site says about itself.
 *
 * Edit this file to change names, links, or the people listed on the site.
 * Nothing else needs touching for those changes.
 */

export const site = {
  name: "Lea",
  fullName: "The Lea Ecosystem",
  tagline: "An agent backbone for mathematician-led formalization.",
  description:
    "Lea is an open-source Lean 4 agent backbone built so the mathematician stays at the center of formalization — steering the decomposition, intervening mid-proof, and reviewing every claim as it is established.",

  // Absolute origin of the deployed site. Used for feed.xml, sitemap.xml and
  // social-preview tags. Change this if you move to a custom domain.
  origin: "https://darturi.github.io",

  // Path the site is served under. GitHub Pages project sites live under
  // /<repo>/; a custom domain or user site would use "/".
  // Can be overridden at build time: `node build.mjs --base=/`
  base: "/UnifiedLeaEcosystem/",

  links: {
    github: "https://github.com/darturi/UnifiedLeaEcosystem",
    githubProver: "https://github.com/darturi/lea-prover",
    discord: "https://discord.gg/CtEJvUTjm",
    issues: "https://github.com/darturi/UnifiedLeaEcosystem/issues",
  },

  // Shown in the footer and on the about strip. Fill these in.
  lab: {
    // TODO(team): set these before the site goes public.
    name: null, // e.g. "The Formal Methods Group"
    institution: null, // e.g. "University of ..."
    contact: null, // e.g. "lea@example.edu"
  },

  nav: [
    { label: "install", href: "/install/" },
    { label: "blog", href: "/blog/" },
    { label: "github", href: "https://github.com/darturi/UnifiedLeaEcosystem", external: true },
    { label: "discord", href: "https://discord.gg/CtEJvUTjm", external: true },
  ],

  // Facts rendered in the hero strip. Keep these true.
  facts: [
    { value: "2", label: "applications on one backbone" },
    { value: "13", label: "models across 3 providers" },
    { value: "Lean 4.29", label: "with Mathlib, pinned" },
  ],
};

export default site;
