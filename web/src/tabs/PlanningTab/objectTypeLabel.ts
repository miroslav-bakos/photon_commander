// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/** Catalogue values are stable machine keys. Planning presents the useful
 * observing class instead of leaking those keys (or over-specific galaxy
 * morphology) into target search and the resolved-target badge. */
const OBJECT_TYPE_LABELS: Readonly<Record<string, string>> = {
  star: "Star",
  double_star: "Double star",
  stellar_association: "Stellar association",
  asterism: "Asterism",
  open_cluster: "Open cluster",
  globular_cluster: "Globular cluster",
  cluster_with_nebula: "Cluster with nebula",
  star_cloud: "Star cloud",
  galaxy: "Galaxy",
  galaxy_spiral: "Galaxy",
  galaxy_elliptical: "Galaxy",
  galaxy_lenticular: "Galaxy",
  galaxy_irregular: "Galaxy",
  galaxy_pair: "Galaxy",
  galaxy_triplet: "Galaxy",
  galaxy_group: "Galaxy",
  planetary_nebula: "Planetary nebula",
  hii_region: "Emission nebula",
  emission_nebula: "Emission nebula",
  reflection_nebula: "Reflection nebula",
  dark_nebula: "Dark nebula",
  diffuse_nebula: "Diffuse nebula",
  supernova_remnant: "Supernova remnant",
  nova: "Nova",
  nonexistent: "Unconfirmed object",
  other: "Other",
  planet: "Planet",
  moon: "Moon",
  comet: "Comet",
};

export function objectTypeLabel(objectType: string): string {
  const known = OBJECT_TYPE_LABELS[objectType];
  if (known !== undefined) return known;
  const words = objectType.replaceAll("_", " ").trim();
  return words.length === 0 ? "Other" : words.charAt(0).toLocaleUpperCase() + words.slice(1);
}
