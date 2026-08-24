# datasets

Open datasets published alongside [Jakub Hałun](https://www.halun.pl)'s applications.

Each application that embeds a derived dataset publishes that dataset here, so the licence terms of
the upstream data are honoured and anyone can inspect, verify or reuse exactly what the app ships.

There is **no repository-wide licence.** Datasets come from different upstreams on different terms;
every dataset directory carries its own licence file and attribution notice. Read the README in the
directory you are interested in before using anything.

## Contents

```
daylight/
└── timezones/    Offline latitude/longitude → IANA time-zone lookup   (ODbL 1.0)
```

### Daylight: Sun & Polar Tracker

An Android app that computes sunrise and sunset times, day-length changes and countdowns to the
solstices, with dedicated handling for polar day and polar night.
More: <https://www.halun.pl/apps/daylight/>

| Dataset | Published here | Derived from | Upstream licence | Published under |
|---|---|---|---|---|
| [`daylight/timezones`](daylight/timezones/) | yes | [timezone-boundary-builder](https://github.com/evansiroky/timezone-boundary-builder) (from OpenStreetMap) | ODbL 1.0 | **ODbL 1.0** |
| GeoNames place database | no — see below | [GeoNames](https://www.geonames.org) `cities5000` | CC BY 4.0 | — |

**`daylight/timezones`** turns a coordinate into an IANA time-zone id offline. It is a simplified,
rasterised derivative of the timezone-boundary-builder `timezones-with-oceans` release. ODbL is a
share-alike licence, so a derivative database that is publicly used has to be offered under ODbL
too — which is what this directory is for.

**The GeoNames place database** (used by the app for offline reverse geocoding and town search) is
*not* republished here. GeoNames is CC BY 4.0: it requires attribution, which the app gives in its
About screen, but it carries no share-alike obligation, so there is nothing this repository is
required to make available. The upstream data is freely downloadable from
<https://download.geonames.org/export/dump/>.

## Attribution

If you use a dataset from this repository, take the attribution notice from that dataset's README —
the wording differs per upstream and, for ODbL, the notice is a licence condition rather than a
courtesy.

## Provenance and verification

Every dataset directory contains:

- the data file itself, exactly as the application ships it;
- `SHA256SUMS.txt`, so a copy extracted from a published app package can be checked byte-for-byte
  against this one;
- the script that produced it from its upstream source, where the dataset is derived rather than
  redistributed verbatim;
- the full text of its licence.

Datasets are regenerated when their upstream publishes a new release. The upstream release each
file was built from is pinned in the dataset's README and recorded inside the data file itself.
