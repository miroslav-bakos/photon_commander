// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import type { components } from "../../api/schema";
import type { BestTargetCategory } from "./planningUiStore";

type ObjectType = components["schemas"]["ObjectType"];
/** Both planners use the owner's category exclusions, including mixed cluster/nebula targets. */
export function objectTypesForCategories(categories: readonly BestTargetCategory[]): ObjectType[] {
  const types = categories.flatMap<ObjectType>((category) => {
    switch (category) {
      case "galaxies": return ["galaxy", "galaxy_spiral", "galaxy_elliptical", "galaxy_lenticular", "galaxy_irregular", "galaxy_pair", "galaxy_triplet", "galaxy_group"];
      case "emission_nebulae": return ["emission_nebula", "hii_region", "diffuse_nebula", ...(categories.includes("star_clusters") ? ["cluster_with_nebula" as const] : [])];
      case "reflection_nebulae": return ["reflection_nebula"];
      case "dark_nebulae": return ["dark_nebula"];
      case "planetary_nebulae": return ["planetary_nebula"];
      case "supernova_remnants": return ["supernova_remnant"];
      case "star_clusters": return ["open_cluster", "globular_cluster", "star_cloud", "stellar_association", "asterism"];
    }
  });
  return [...new Set(types)];
}
