import argparse
import csv
import logging
import os
import shutil
from tempfile import NamedTemporaryFile
from typing import TextIO

import pandas as pd

from dadis_client import DadisClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def full_matching_workflow(
    input_filename: str, output_filename: str, dadis_api_key: str
) -> pd.DataFrame:
    """
    Perform the full matching workflow:

    - Read VBO data from input_filename
    - Match to DADIS to get dadis_transboundary_id
    - Save to a new TSV file at output_filename
    """
    client = DadisClient(api_key=dadis_api_key)
    vbo_data = read_vbo_data(input_filename)
    matched_breeds = match_vbo_breeds(vbo_data=vbo_data, client=client)

    # We need to create a temporary file for the output, in case we're writing
    #   to the same filename as the input: we don't want
    #   to overwrite/clear the input file until we can read the header from it
    logger.info(f"Creating temporary output TSV:")
    with NamedTemporaryFile(mode="w", delete=False) as temp_out:
        write_tsv_header(
            input_filename=input_filename,
            output_file=temp_out,
            extra_cols=["dadis_transboundary_id"],
        )
        matched_breeds = clean_output(matched_breeds)
        # Write the actual data below the headers
        matched_breeds.to_csv(temp_out, sep="\t", index=False, header=False)
        temp_out.close()
        logger.info("Output written to temp file.")
        shutil.copy2(src=temp_out.name, dst=output_filename)
        logger.info(f"Copied to {output_filename}")
        return matched_breeds


def read_vbo_data(filename: str) -> pd.DataFrame:
    df = pd.read_table(
        filename,
        skiprows=[1],
        na_values=[],
        keep_default_na=False,
        dtype={"obsolete": str},
    ).convert_dtypes(
        infer_objects=False,
        convert_string=False,
        convert_boolean=False
    )
    return df


def get_dadis_species(client: DadisClient) -> pd.DataFrame:
    resp = client.get_all_species()
    all_species = []
    for s in resp.response:
        species = {"dadis_species_id": s.id, "dadis_species_name": s.name["en"]}
        all_species.append(species)
    return pd.DataFrame.from_records(all_species)


def get_canonical_dadis_transboundary(client: DadisClient) -> pd.DataFrame:
    """
    DADIS has a canonical name for each transboundary breed, fetch these
    and return them as a dataframe
    """
    resp = client.get_all_transboundary_names()
    df = pd.DataFrame.from_records([b.model_dump() for b in resp.response]).rename(
        columns={"speciesId": "dadis_species_id"}
    )
    species_df = get_dadis_species(client)
    df = df.merge(species_df, how="left", on="dadis_species_id")
    df = df.rename(columns={"id": "dadis_transboundary_id", "name": "dadis_breed_name"})
    return df


def get_all_dadis_transboundary(client: DadisClient) -> pd.DataFrame:
    """
    Get all names for DADIS transboundary breeds, some VBO entries
    may use non-canonical names
    """
    resp = client.get_all_transboundary_breeds()
    df = (
        pd.DataFrame.from_records([b.model_dump() for b in resp.response])
        .rename(
            columns={
                "speciesId": "dadis_species_id",
                "name": "dadis_breed_name",
                "transboundaryId": "dadis_transboundary_id",
                "id": "dadis_breed_id",
                "iso3": "dadis_iso3_code",
            }
        )
        .drop_duplicates(
            subset=["dadis_species_id", "dadis_breed_name", "dadis_transboundary_id"]
        )
    )
    species_df = get_dadis_species(client)
    result = df.merge(species_df, how="left", on="dadis_species_id").sort_values(
        ["dadis_transboundary_id", "dadis_breed_name"]
    )
    return result


def get_simple_matches(vbo_data: pd.DataFrame, client: DadisClient) -> pd.DataFrame:
    """
    Match VBO entries to DADIS transboundary breeds based on their
    canonical names. Return a dataframe containing the matches
    """
    dadis_canonical = get_canonical_dadis_transboundary(client=client)
    # Ignore any VBO entries marked as duplicate while matching
    match_data = vbo_data.query("to_be_ignored != 'duplicate'")[
        ["vbo_id", "term_label", "dadis_name", "dadis_species_name"]
    ]
    simple_matches = (
        match_data.merge(
            dadis_canonical,
            how="left",
            left_on=["dadis_name", "dadis_species_name"],
            right_on=["dadis_breed_name", "dadis_species_name"],
            sort=False,
        )
        .drop(columns=["dadis_breed_name", "dadis_species_name"])
        .convert_dtypes()
    )
    return simple_matches


def get_extra_matches(vbo_data: pd.DataFrame, client: DadisClient) -> pd.DataFrame:
    dadis_all = get_all_dadis_transboundary(client=client)
    match_data = vbo_data.query("to_be_ignored != 'duplicate'")[
        ["vbo_id", "term_label", "dadis_name", "dadis_species_name"]
    ]
    extra_matches = (
        match_data.merge(
            dadis_all,
            how="left",
            left_on=["dadis_name", "dadis_species_name"],
            right_on=["dadis_breed_name", "dadis_species_name"],
            indicator=True,
            # Need to ensure we use sort=False so order stays consistent with original
            sort=False,
        )
        .drop(columns=["dadis_breed_name", "dadis_species_name"])
        .convert_dtypes()
        .drop_duplicates(subset=["vbo_id", "dadis_transboundary_id"])
    )
    counts = extra_matches["vbo_id"].value_counts()
    duplicates = counts.loc[counts >= 2].index.tolist()
    multiple_matches = extra_matches.loc[extra_matches["vbo_id"].isin(duplicates), :]
    n_multiple = multiple_matches["vbo_id"].nunique()
    logger.info(
        f"{n_multiple} VBO entries matched against multiple DADIS entries - will not be updated.  Use --log=DEBUG to see them."
    )
    logger.debug(multiple_matches["vbo_id"])
    extra_matches = extra_matches.loc[~extra_matches["vbo_id"].isin(duplicates), :]
    return extra_matches


def match_vbo_breeds(vbo_data: pd.DataFrame, client: DadisClient) -> pd.DataFrame:
    """
    Match VBO entries to DADIS, based on both canonical name (preferred) or
    other transboundary name.

    Return a modified copy of vbo_data, with the 'dadis_transboundary_id' added
    """
    logger.info("Matching to canonical DADIS names")
    simple_matches = get_simple_matches(vbo_data, client)
    logger.info("Matching to other DADIS names")
    extra_matches = get_extra_matches(vbo_data, client)
    all_matches = simple_matches.merge(
        extra_matches[["vbo_id", "dadis_transboundary_id", "dadis_species_id"]],
        how="left",
        on="vbo_id",
        suffixes=(None, "_extra"),
        sort=False,
    ).convert_dtypes()
    # Fill in simple matches with extra matches, where no simple match was found
    simple_ids = all_matches["dadis_transboundary_id"]
    extra_ids = all_matches["dadis_transboundary_id_extra"]
    all_matches["dadis_transboundary_id"] = simple_ids.where(
        ~simple_ids.isna(), extra_ids
    )

    n_total = all_matches.shape[0]
    n_matched = all_matches["dadis_transboundary_id"].notna().sum()
    logger.info(f"{n_matched} / {n_total} VBO entries matched with DADIS")

    return vbo_data.merge(
        all_matches[["vbo_id", "dadis_transboundary_id"]],
        on="vbo_id",
        how="left",
        sort=False,
    )


def write_tsv_header(
    input_filename: str, output_file: TextIO, extra_cols: list[str] = None
):
    """
    Copy the two header lines from the input file to output_file, adding the columns in extra_cols
    """
    csv_out = csv.writer(output_file, dialect="excel-tab")
    with open(input_filename) as file_in:
        csv_in = csv.reader(file_in, dialect="excel-tab")
        for index, line in enumerate(range(2)):
            header = next(csv_in)
            if extra_cols is not None:
                if index == 0:
                    header += extra_cols
                if index == 1:
                    header += ["" for i in range(len(extra_cols))]
            csv_out.writerow(header)


def clean_output(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean the dataframe before writing

    * Convert any None values to empty strings
    """
    string_columns = df.select_dtypes(include="object").columns
    for column in string_columns:
        df[column] = df[column].fillna("")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find DADIS entries matching VBO breeds"
    )
    parser.add_argument("--log", help="Logging level", default="INFO")
    parser.add_argument(
        "--input_filename", help="Spreadsheet (TSV) with VBO transboundary breeds"
    )
    parser.add_argument("--output_filename", help="Filename to save the updated TSV to")
    parser.add_argument(
        "--dadis_api_key",
        help="API key for DADIS API (private: should be stored in Github Secrets)",
        default=os.getenv("DADIS_API_KEY")
    )
    args = parser.parse_args()

    if args.dadis_api_key is None:
        raise ValueError("DADIS API key not set. Set the DADIS_API_KEY environment variable or use the --dadis_api_key argument")

    logger.setLevel(args.log.upper())
    full_matching_workflow(
        input_filename=args.input_filename,
        output_filename=args.output_filename,
        dadis_api_key=args.dadis_api_key,
    )
