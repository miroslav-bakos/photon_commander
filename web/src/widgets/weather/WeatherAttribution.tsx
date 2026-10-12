// SPDX-License-Identifier: GPL-3.0-only

const credits: Record<string, { label: string; url: string }> = {
  weatherapi: { label: "Weather data by WeatherAPI.com", url: "https://www.weatherapi.com/" },
  visualcrossing: {
    label: "Weather Data Provided by Visual Crossing",
    url: "https://www.visualcrossing.com/",
  },
};

/** Required provider credit, placed beside raw weather or a derived verdict. */
export function WeatherAttribution({ source }: { source: string | null | undefined }) {
  const credit = source === null || source === undefined ? undefined : credits[source];
  if (credit === undefined) return null;
  return (
    <a className="text-xs text-link hover:underline" href={credit.url} target="_blank" rel="noreferrer">
      {credit.label}
    </a>
  );
}
