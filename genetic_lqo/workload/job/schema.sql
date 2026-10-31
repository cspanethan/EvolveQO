--
-- PostgreSQL database dump
--

-- Dumped from database version 10.5
-- Dumped by pg_dump version 11.1

--
-- Name: aka_name; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE aka_name (
    id integer NOT NULL PRIMARY KEY,
    person_id integer NOT NULL REFERENCES name (id),
    name nvarchar(max),
    imdb_index nvarchar(3),
    name_pcode_cf nvarchar(11),
    name_pcode_nf nvarchar(11),
    surname_pcode nvarchar(11),
    md5sum nvarchar(65)
);


--
-- Name: aka_title; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE aka_title (
    id integer NOT NULL PRIMARY KEY,
    movie_id integer NOT NULL REFERENCES title (id),
    title nvarchar(max),
    imdb_index nvarchar(4),
    kind_id integer NOT NULL REFERENCES kind_type (id),
    production_year integer,
    phonetic_code nvarchar(5),
    episode_of_id integer,
    season_nr integer,
    episode_nr integer,
    note nvarchar(72),
    md5sum nvarchar(32)
);


--
-- Name: cast_info; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE cast_info (
    id integer NOT NULL PRIMARY KEY,
    person_id integer NOT NULL REFERENCES name (id),
    movie_id integer NOT NULL REFERENCES title (id),
    person_role_id integer REFERENCES char_name (id),
    note nvarchar(max),
    nr_order integer,
    role_id integer NOT NULL REFERENCES role_type (id)
);


--
-- Name: char_name; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE char_name (
    id integer NOT NULL PRIMARY KEY,
    name nvarchar(max) NOT NULL,
    imdb_index nvarchar(2),
    imdb_id integer,
    name_pcode_nf nvarchar(5),
    surname_pcode nvarchar(5),
    md5sum nvarchar(32)
);


--
-- Name: comp_cast_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE comp_cast_type (
    id integer NOT NULL PRIMARY KEY,
    kind nvarchar(32) NOT NULL
);


--
-- Name: company_name; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE company_name (
    id integer NOT NULL PRIMARY KEY,
    name nvarchar(max) NOT NULL,
    country_code nvarchar(6),
    imdb_id integer,
    name_pcode_nf nvarchar(5),
    name_pcode_sf nvarchar(5),
    md5sum nvarchar(32)
);


--
-- Name: company_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE company_type (
    id integer NOT NULL PRIMARY KEY,
    kind nvarchar(32)
);


--
-- Name: complete_cast; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE complete_cast (
    id integer NOT NULL PRIMARY KEY,
    movie_id integer REFERENCES title (id),
    subject_id integer NOT NULL REFERENCES comp_cast_type (id),
    status_id integer NOT NULL REFERENCES comp_cast_type (id)
);


--
-- Name: info_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE info_type (
    id integer NOT NULL PRIMARY KEY,
    info nvarchar(32) NOT NULL
);


--
-- Name: keyword; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE keyword (
    id integer NOT NULL PRIMARY KEY,
    keyword nvarchar(max) NOT NULL,
    phonetic_code nvarchar(5)
);


--
-- Name: kind_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE kind_type (
    id integer NOT NULL PRIMARY KEY,
    kind nvarchar(15)
);


--
-- Name: link_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE link_type (
    id integer NOT NULL PRIMARY KEY,
    link nvarchar(32) NOT NULL
);


--
-- Name: m_movie_info; Type: TABLE; Schema: public; Owner: -
--

-- CREATE TABLE m_movie_info (
--     id integer PRIMARY KEY,
--     movie_id integer REFERENCES title (id),
--     info_type_id integer REFERENCES info_type (id),
--     minfo integer,
--     info nvarchar(max)
-- );


--
-- Name: movie_companies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE movie_companies (
    id integer NOT NULL PRIMARY KEY,
    movie_id integer NOT NULL REFERENCES title (id),
    company_id integer NOT NULL REFERENCES company_name (id),
    company_type_id integer NOT NULL REFERENCES company_type (id),
    note nvarchar(max)
);


--
-- Name: movie_info; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE movie_info (
    id integer NOT NULL PRIMARY KEY,
    movie_id integer NOT NULL REFERENCES title (id),
    info_type_id integer NOT NULL REFERENCES info_type (id),
    info nvarchar(max) NOT NULL,
    note nvarchar(max)
);


--
-- Name: movie_info_idx; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE movie_info_idx (
    id integer NOT NULL PRIMARY KEY,
    movie_id integer NOT NULL REFERENCES title (id),
    info_type_id integer NOT NULL REFERENCES info_type (id),
    info nvarchar(max) NOT NULL,
    note nvarchar(max)
);


--
-- Name: movie_keyword; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE movie_keyword (
    id integer NOT NULL PRIMARY KEY,
    movie_id integer NOT NULL REFERENCES title (id),
    keyword_id integer NOT NULL REFERENCES keyword (id)
);


--
-- Name: movie_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE movie_link (
    id integer NOT NULL PRIMARY KEY,
    movie_id integer NOT NULL REFERENCES title (id),
    linked_movie_id integer NOT NULL REFERENCES title (id),
    link_type_id integer NOT NULL REFERENCES link_type (id)
);


--
-- Name: name; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE name (
    id integer NOT NULL PRIMARY KEY,
    name nvarchar(max) NOT NULL,
    imdb_index nvarchar(9),
    imdb_id integer,
    gender nvarchar(1),
    name_pcode_cf nvarchar(5),
    name_pcode_nf nvarchar(5),
    surname_pcode nvarchar(5),
    md5sum nvarchar(32)
);


--
-- Name: person_info; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE person_info (
    id integer NOT NULL PRIMARY KEY,
    person_id integer NOT NULL REFERENCES name (id),
    info_type_id integer NOT NULL REFERENCES info_type (id),
    info nvarchar(max) NOT NULL,
    note nvarchar(max)
);


--
-- Name: role_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE role_type (
    id integer NOT NULL PRIMARY KEY,
    role nvarchar(32) NOT NULL
);


--
-- Name: title; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE title (
    id integer NOT NULL PRIMARY KEY,
    title nvarchar(max) NOT NULL,
    imdb_index nvarchar(5),
    kind_id integer NOT NULL REFERENCES kind_type (id),
    production_year integer,
    imdb_id integer,
    phonetic_code nvarchar(5),
    episode_of_id integer,
    season_nr integer,
    episode_nr integer,
    series_years nvarchar(49),
    md5sum nvarchar(32)
);


--
-- PostgreSQL database dump complete
--

