"""Bundle writers and binary codecs (ARCHITECTURE.md section 5; docs/bundle.md).

* :mod:`.writer`  -- content-hashed file naming, atomic writes, staging-root guard, byte ledger;
* :mod:`.codec`   -- column encodings (u16q, u16log, u8q, u8log, cat8/16, bits, f32, u32,
  u32lin, i16off, str, sparse), byte shuffles and the deterministic gzip writer;
* :mod:`.order`   -- row order / LOD permutation;
* :mod:`.catalog` -- ``pk-catalog/1`` manifest + column files;
* :mod:`.records` -- ``pk-records/1`` index, pks1 / pki1 / json / sedcurve records, shards;
* :mod:`.density` -- pre-binned density and binned-statistic grids;
* :mod:`.images`  -- ``pk-images/1`` documents.
"""

from __future__ import annotations

from . import catalog, codec, density, images, order, records, writer
from .catalog import CatalogResult
from .images import ImagesResult
from .order import RowOrder, make_order
from .records import RecordsResult, ShardPacker, decode_pki1, decode_pks1, encode_pki1, encode_pks1
from .writer import BundleError, BundleWriter, Ledger, StagingError, dumps_json, h8, hashed_name

__all__ = [
    "catalog", "codec", "density", "images", "order", "records", "writer",
    "CatalogResult", "ImagesResult", "RecordsResult", "RowOrder", "make_order", "ShardPacker",
    "encode_pks1", "decode_pks1", "encode_pki1", "decode_pki1",
    "BundleError", "BundleWriter", "Ledger", "StagingError", "dumps_json", "h8", "hashed_name",
    "write_catalog", "write_records", "write_images",
]

write_catalog = catalog.write
write_records = records.write
write_images = images.write
