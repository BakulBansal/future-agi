// Cell text for the Connected agents table. Every value comes from the API
// as sent — nothing here limits which modalities or sources can appear.

const EMPTY = "—";

export const humanize = (value) =>
  String(value)
    .split(/[_-]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");

export const modalityLabel = ({ modality, direction }) => {
  if (!modality) return EMPTY;
  return direction
    ? `${humanize(modality)} · ${humanize(direction)}`
    : humanize(modality);
};

export const sourceLabel = ({ source, sourceLabel: label }) => {
  if (label) return label;
  return source ? humanize(source) : EMPTY;
};

export const usedInLabel = (usedIn) => {
  const envs = usedIn || [];
  if (envs.length === 0) return { text: "No environments yet", empty: true };
  if (envs.length === 1) return { text: envs[0].name, empty: false };
  return { text: `${envs.length} environments`, empty: false };
};

export const versionsParts = ({ latestLabel, versionCount }) =>
  latestLabel
    ? { version: latestLabel, rest: ` · ${versionCount} total` }
    : { version: "", rest: `${versionCount} total` };
