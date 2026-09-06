# Colour calibration: catalogues, and the per-project sky region

How white balancing gets its reference data, and what to download per
project.

Colour calibration is **SPCC only**. An earlier version of this pipeline
used PCC (Siril's broadband method, keyed to an online VizieR query) and
supported both, selectable via a `colour_calibration` parameter. PCC has
since been removed: SPCC won the comparison run on real M51/T24 data (see
below) and has no runtime dependency on a third-party server, where PCC's
VizieR dependency returned HTTP 403 for hours during development and
blocked the pipeline entirely.

## The intended workflow

For each new project, download only the sky region you are imaging, then
calibrate against it locally. No dependency on a third-party server at
processing time.

SPCC's catalogue comes from Gaia DR3, repackaged for Siril and hosted on
[Zenodo](https://zenodo.org/records/14738271) as 48 files, chunked by sky
region (level-1 HEALPix, ~859 square degrees each) -- **10.6 GB total**,
but a project only needs the chunk(s) covering its own target.

(There is a second, unrelated Gaia catalogue -- an "astrometric" extract,
one whole-sky 1.1 GB file -- that only PCC used, via `pcc
-catalog=localgaia`. It is not needed for anything in this codebase now
that PCC is gone.)

## Finding your chunk

`astropy_healpix` is already a dependency:

```python
from astropy_healpix import HEALPix
import astropy.units as u
hp = HEALPix(nside=2, order="nested")
chunk = int(hp.lonlat_to_healpix(ra_deg * u.deg, dec_deg * u.deg))
```

Worked examples:

| target | RA | Dec | chunk | file size |
|---|---|---|---|---|
| M51 | 202.470 | +47.195 | **10** | 49.4 MB |
| M31 | 10.685 | +41.269 | 2 | |
| M13 | 250.423 | +36.461 | 9 | 101.6 MB |
| M42 | 83.822 | -5.391 | 20 | |

A target near a chunk boundary, or a wide mosaic, may need the neighbouring
chunk as well -- to check, cone-search the chunks the field actually
touches:

```python
sorted(int(c) for c in hp.cone_search_lonlat(ra*u.deg, dec*u.deg, 1.5*u.deg))
```

If that returns more than one chunk, download them all.

## Installing a chunk

Siril expects a **directory**, despite the `.dat` name — the default
setting `~/.local/share/siril/gaia_photometric.dat` is a folder that
Siril iterates, not a file. Getting this wrong produces
"directory iterator cannot open directory".

```
curl -L -o chunk10.dat.bz2 \
  "https://zenodo.org/records/14738271/files/siril_cat1_healpix8_xpsamp_10.dat.bz2?download=1"
bunzip2 chunk10.dat.bz2
mkdir -p "%LOCALAPPDATA%/siril/gaia_photometric.dat"
mv chunk10.dat "%LOCALAPPDATA%/siril/gaia_photometric.dat/siril_cat1_healpix8_xpsamp_10.dat"
```

Then point Siril at it (persists in config, so this is one-time):

```
set core.catalogue_gaia_photo=C:/Users/<you>/AppData/Local/siril/gaia_photometric.dat
```

Chunk 10 is installed on this machine and Siril reads it — the log line
`Getting stars from local catalogue Gaia DR3 xp_sampled for SPCC`
confirms it, so the install procedure above is verified working.

## Sensor and filter definitions

SPCC also needs to know the sensor and filters, to model spectral
response. Those live in a separate repository that Siril syncs from its
GUI (`gui.use_spcc_repository`); it is **not** fetched in headless CLI
use. Clone it manually:

```
git clone --depth 1 https://gitlab.com/free-astro/siril-spcc-database.git \
  "%LOCALAPPDATA%/siril/siril-spcc-database"
```

Names must match the JSON `name` field exactly, and contain spaces, so
they need quoting in a `.ssf` script. For T24 (Apogee, 9 um pixels,
4096x4096 -> KAF-16803, Astrodon filters):

```
spcc -catalog=localgaia "-monosensor=KAF16803" "-rfilter=Astrodon Red (E series)" \
     "-gfilter=Astrodon Green (E series)" "-bfilter=Astrodon Blue (E / I series)"
```

These are the `InstrumentProfile` entries in `background_color.py`
(`INSTRUMENT_PROFILES`, keyed by telescope). Add a profile there for each
new telescope/sensor combination.

## Status: works, requires Siril >= 1.4.4

On Siril 1.4.3, SPCC crashed the process outright with an access
violation (`0xC0000005`), always at
`Applying aperture photometry to 73 stars`, regardless of catalogue source
or sensor/filter configuration. Upgrading to 1.4.4 fixed it -- despite
1.4.4's changelog mentioning nothing about SPCC, photometry, or colour
calibration. Three hypotheses about the crash were tested against 1.4.3
and all three were wrong (it was not an online-catalogue fallback, and not
unset sensor/filters); the answer was simply the version. A test
(`test_siril_version_meets_spcc_minimum`) pins the requirement so a
downgrade fails loudly instead of crashing the process.

Verified working on the real M51/T24 data:

```
SPCC will use mono senor "KAF16803" and filters "Astrodon Red (E series)", ...
Getting stars from local catalogue Gaia DR3 xp_sampled for SPCC
Applying aperture photometry to 73 stars.
30 stars excluded from the calculation
Found a solution for color calibration using 43 stars.
K0: 0.925  K1: 0.899  K2: 1.000
Spectrophotometric Color Calibration succeeded.
```

### Expect relatively few stars

SPCC used **43** stars on the M51/T24 data (PCC, before removal, used
**246** on the same data). That is normal, not a coverage gap -- SPCC can
only use stars that have Gaia XP *sampled spectra*, a much smaller
population than plain broadband photometry. Checked explicitly for this
field: it lies entirely within chunk 10 even out to a 3-degree radius, so
nothing is missing. If star count looks unexpectedly low on a new target,
suspect a chunk-boundary gap (see "Finding your chunk" above) before
suspecting SPCC.

### Why SPCC over PCC

The comparison that motivated dropping PCC, on the same M51/T24 composite:

| | PCC | SPCC |
|---|---|---|
| stars used | 246 | 43 |
| white balance | (0.533, 0.684, 1.0) | (0.925, 0.899, 1.0) |
| catalogue | VizieR, online | local chunk, offline |

SPCC's factors sit near unity because it models the actual spectral
response of the sensor and filters rather than inferring from broadband
colour. Visually the difference was real on the rendered image: NGC 5195's
core rendered warm against M51's bluer star-forming arms under SPCC, where
PCC left both grey-cyan.
