from shutil import Error
from zipfile import ZipFile
import lxml
import numpy as np
import pandas as pd
import sqlalchemy
import sqlite3
from sqlalchemy.sql.expression import table
from open_mastr.soap_api.orm import tablename_mapping
import open_mastr.soap_api.orm as orm
import pdb
from sqlalchemy import (
    Integer,
    String,
    Float,
    DateTime,
    Boolean,
    Date,
    JSON,
)

dtypes_mapping = {
    String: "string",
    Integer: "int",
    Float: "float",
    DateTime: "datetime64[s]",
    Boolean: "bool",
    Date: "datetime64[s]",
    JSON: "string"
}


def convert_mastr_xml_to_sqlite(
    con: sqlite3.Connection,
    zipped_xml_file_path: str,
    include_tables: list,
    engine,
) -> None:
    """Converts the Mastr in xml format into a sqlite database."""
    """Writes the local zipped MaStR to a PostgreSQL database.

    Parameters
    ------------
    include_tables : list, default None
        List of tables from the Marktstammdatenregister that
        should be written into the database. Elements of
        include_tables are lower case strings without "_" and index.
        It is possible to include any table from the zipped
        local MaStR folder (see MaStR.initialize()).
        Example: If you do want to write the data from
        files "AnlagenEegSolar_*.xml" to a table in your
        database (where * is any number >=1), write the
        element "anlageneegsolar" into the include_tables list.
        Elements of the list that cannot be matched to tables of
        the MaStR are ignored. If include_tables is given,
        only the tables listed here are written to the database.


    """

    with ZipFile(zipped_xml_file_path, "r") as f:
        for file_name in f.namelist():
            # xml_tablename is the beginning of the filename without the number in lowercase
            xml_tablename = file_name.split("_")[0].split(".")[0].lower()

            # few tables are only needed for data cleansing of the xml files and contain no 
            # information of relevance
            boolean_write_table_to_sql_database = False
            if tablename_mapping[xml_tablename]["__class__"] != None:
                boolean_write_table_to_sql_database = True

            # check if the table should be written to sql database (depends on user input)
            include_count = include_tables.count(xml_tablename)

            if include_count == 1 and boolean_write_table_to_sql_database:

                sql_tablename = tablename_mapping[xml_tablename]["__name__"]

                if (
                    file_name.split(".")[0].split("_")[-1] == "1"
                    or len(file_name.split(".")[0].split("_")) == 1
                ):
                    sqlalchemy.delete(table(sql_tablename))
                    print(
                        f"Table '{sql_tablename}' is filled with data '{xml_tablename}' "
                        "from the bulk download."
                    )
                    index_for_printed_message = 1
                else:
                    print(
                        f"File {index_for_printed_message} from '{xml_tablename}' is parsed."
                    )
                    index_for_printed_message += 1

                add_table_to_sqlite_database(
                    f, file_name, sql_tablename, xml_tablename, con=con, engine=engine
                )


def add_table_to_sqlite_database(
    f: ZipFile,
    file_name: str,
    sql_tablename: str,
    xml_tablename: str,
    con: sqlite3.Connection,
    engine,
) -> None:
    data = f.read(file_name)
    try:
        df = pd.read_xml(data, encoding="UTF-16", compression="zip")
    except lxml.etree.XMLSyntaxError as err:
        df = handle_xml_syntax_error(data, err)

    continueloop = True
    if tablename_mapping[xml_tablename]["replace_column_names"]:
    
        df = df.rename(columns=tablename_mapping[xml_tablename]["replace_column_names"])

    # get a dictionary for the data types

    #dtypes_for_writing_sql = {}
    #dtypes_for_converting_dataframe = {}

    
    #for column in list(tablename_mapping[xml_tablename]["__class__"].__table__.columns):
    #    if column.name in df.columns:
    #        dtypes_for_writing_sql[column.name] = column.type
    #        dtypes_for_converting_dataframe[column.name] = dtypes_mapping[type(column.type)]
    #try:
    #    df = df.astype("string")#dtype=dtypes_for_converting_dataframe, copy=False)
    #except pd._libs.tslibs.np_datetime.OutOfBoundsDatetime:
        # The datetimes are given in nanoseconds with exceeds the possible range
        # Hence the nanosecond and milisecond information is dropped
    #    df["DatumLetzteAktualisierung"] = df["DatumLetzteAktualisierung"].str[:-8]
    #    pdb.set_trace()
    #    df = df.astype(dtype=dtypes_for_converting_dataframe, copy=False)
    
        # the file einheitentypen.xml can be ignored
    while continueloop and file_name != "Einheitentypen.xml":
        
        try:
            df.to_sql(sql_tablename, con=engine, index=False, if_exists="append")#, dtype=dtypes_for_writing_sql)
            continueloop = False
        except sqlalchemy.exc.OperationalError as err:
            add_missing_column_to_table(err, con, sql_tablename)

        except sqlalchemy.exc.DataError as err:
            delete_wrong_xml_entry(err, df)

        
            #delete_nanosecond_information(err, df)

        # except sqlite3.IntegrityError as err:
        # error resulting from NOT NULL constraint failed
        # meaning that a unique identifier is not unique
        #    delete_not_unique_entry(err, df)


# def delete_not_unique_entry(err: Error, df: pd.DataFrame) -> None:
#    column_with_unique_entries = str(err).split("constraint failed: ")[1].split(".")[1]
#    pdb.set_trace()
# df


def add_missing_column_to_table(
    err: Error, con: sqlite3.Connection, sql_tablename: str
) -> None:
    """Some files introduce new columns for existing tables.
    If this happens, the error from writing entries into
    non-existing columns is caught and the column is created."""

    # Needed for sqlite3 error message
    # missing_column = str(err).split("no column named ")[1]

    # Needed for sqlalchemy error message
    missing_column = str(err).split("has no column named ")[1].split("\n[SQL: ")[0]

    cursor = con.cursor()

    execute_message = 'ALTER TABLE %s ADD "%s" text NULL;' % (
        sql_tablename,
        missing_column,
    )
    cursor.execute(execute_message)
    con.commit()
    cursor.close()


def delete_wrong_xml_entry(err: Error, df: pd.DataFrame) -> None:
    delete_entry = str(err).split("«")[0].split("»")[1]
    print(f"The entry {delete_entry} was deleteted due to its false data type.")
    df = df.replace(delete_entry, np.nan)


def handle_xml_syntax_error(data: bytes, err: Error) -> pd.DataFrame:
    """Deletes entries that cause an xml syntax error and produces DataFrame.

    Parameters
    -----------
    data : bytes
        Unzipped xml data
    err : ErrorMessage
        Error message that appeared when trying to use pd.read_xml on invalid xml file.

    Returns
    ----------
    df : pandas.DataFrame
        DataFrame which is read from the changed xml data.
    """

    # Actually it is unclear if there are still invalid xml files in the recent MaStR.

    wrong_char_position = int(str(err).split()[-4])
    decoded_data = data.decode("utf-16")
    loop_condition = True

    shift = 0
    while loop_condition:
        evaluated_string = decoded_data[wrong_char_position + shift]
        if evaluated_string == ">":
            start_char = wrong_char_position + shift + 1
            break
        else:
            shift -= 1
    loop_condition_2 = True
    while loop_condition_2:
        evaluated_string = decoded_data[start_char]
        if evaluated_string == "<":
            break
        else:
            decoded_data = decoded_data[:start_char] + decoded_data[start_char + 1:]
    df = pd.read_xml(decoded_data)
    print("One invalid xml expression was deleted.")
    return df
