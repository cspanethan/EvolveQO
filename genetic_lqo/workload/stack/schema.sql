--
-- PostgreSQL database dump
--

-- Dumped from database version 14.4 (Debian 14.4-1.pgdg110+1)
-- Dumped by pg_dump version 16.2

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: postgres
--

-- *not* creating schema, since initdb creates it


ALTER SCHEMA public OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: account; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.account (
    id integer NOT NULL,
    display_name character varying,
    location character varying,
    about_me character varying,
    website_url character varying
);


ALTER TABLE public.account OWNER TO so;

--
-- Name: answer; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.answer (
    id integer NOT NULL,
    site_id integer NOT NULL,
    question_id integer,
    creation_date timestamp without time zone,
    deletion_date timestamp without time zone,
    score integer,
    view_count integer,
    body character varying,
    owner_user_id integer,
    last_editor_id integer,
    last_edit_date timestamp without time zone,
    last_activity_date timestamp without time zone,
    title character varying
);


ALTER TABLE public.answer OWNER TO so;

--
-- Name: badge; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.badge (
    site_id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying NOT NULL,
    date timestamp without time zone NOT NULL
);


ALTER TABLE public.badge OWNER TO so;

--
-- Name: comment; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.comment (
    id integer NOT NULL,
    site_id integer NOT NULL,
    post_id integer,
    user_id integer,
    score integer,
    body character varying,
    date timestamp without time zone
);


ALTER TABLE public.comment OWNER TO so;

--
-- Name: post_link; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.post_link (
    site_id integer NOT NULL,
    post_id_from integer NOT NULL,
    post_id_to integer NOT NULL,
    link_type integer NOT NULL,
    date timestamp without time zone
);


ALTER TABLE public.post_link OWNER TO so;

--
-- Name: question; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.question (
    id integer NOT NULL,
    site_id integer NOT NULL,
    accepted_answer_id integer,
    creation_date timestamp without time zone,
    deletion_date timestamp without time zone,
    score integer,
    view_count integer,
    body character varying,
    owner_user_id integer,
    last_editor_id integer,
    last_edit_date timestamp without time zone,
    last_activity_date timestamp without time zone,
    title character varying,
    favorite_count integer,
    closed_date timestamp without time zone,
    tagstring character varying
);


ALTER TABLE public.question OWNER TO so;

--
-- Name: site; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.site (
    site_id integer NOT NULL,
    site_name character varying
);


ALTER TABLE public.site OWNER TO so;

--
-- Name: so_user; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.so_user (
    id integer NOT NULL,
    site_id integer NOT NULL,
    reputation integer,
    creation_date timestamp without time zone,
    last_access_date timestamp without time zone,
    upvotes integer,
    downvotes integer,
    account_id integer
);


ALTER TABLE public.so_user OWNER TO so;

--
-- Name: tag; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.tag (
    id integer NOT NULL,
    site_id integer NOT NULL,
    name character varying
);


ALTER TABLE public.tag OWNER TO so;

--
-- Name: tag_question; Type: TABLE; Schema: public; Owner: so
--

CREATE TABLE public.tag_question (
    question_id integer NOT NULL,
    tag_id integer NOT NULL,
    site_id integer NOT NULL
);


ALTER TABLE public.tag_question OWNER TO so;

--
-- Name: account account_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_pkey PRIMARY KEY (id);


--
-- Name: answer answer_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.answer
    ADD CONSTRAINT answer_pkey PRIMARY KEY (id, site_id);


--
-- Name: badge badge_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.badge
    ADD CONSTRAINT badge_pkey PRIMARY KEY (site_id, user_id, name, date);


--
-- Name: comment comment_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.comment
    ADD CONSTRAINT comment_pkey PRIMARY KEY (id, site_id);


--
-- Name: post_link post_link_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.post_link
    ADD CONSTRAINT post_link_pkey PRIMARY KEY (site_id, post_id_from, post_id_to, link_type);


--
-- Name: question question_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.question
    ADD CONSTRAINT question_pkey PRIMARY KEY (id, site_id);


--
-- Name: site site_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.site
    ADD CONSTRAINT site_pkey PRIMARY KEY (site_id);


--
-- Name: so_user so_user_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.so_user
    ADD CONSTRAINT so_user_pkey PRIMARY KEY (id, site_id);


--
-- Name: tag tag_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT tag_pkey PRIMARY KEY (id, site_id);


--
-- Name: tag_question tag_question_pkey; Type: CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.tag_question
    ADD CONSTRAINT tag_question_pkey PRIMARY KEY (site_id, question_id, tag_id);


--
-- Name: answer_creation_date_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX answer_creation_date_idx ON public.answer USING btree (creation_date);


--
-- Name: answer_last_editor_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX answer_last_editor_id_idx ON public.answer USING btree (last_editor_id);


--
-- Name: answer_owner_user_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX answer_owner_user_id_idx ON public.answer USING btree (owner_user_id);


--
-- Name: answer_site_id_question_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX answer_site_id_question_id_idx ON public.answer USING btree (site_id, question_id);


--
-- Name: comment_site_id_post_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX comment_site_id_post_id_idx ON public.comment USING btree (site_id, post_id);


--
-- Name: comment_site_id_user_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX comment_site_id_user_id_idx ON public.comment USING btree (site_id, user_id);


--
-- Name: question_creation_date_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX question_creation_date_idx ON public.question USING btree (creation_date);


--
-- Name: question_last_editor_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX question_last_editor_id_idx ON public.question USING btree (last_editor_id);


--
-- Name: question_owner_user_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX question_owner_user_id_idx ON public.question USING btree (owner_user_id);


--
-- Name: so_user_creation_date_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX so_user_creation_date_idx ON public.so_user USING btree (creation_date);


--
-- Name: so_user_last_access_date_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX so_user_last_access_date_idx ON public.so_user USING btree (last_access_date);


--
-- Name: tag_question_site_id_tag_id_question_id_idx; Type: INDEX; Schema: public; Owner: so
--

CREATE INDEX tag_question_site_id_tag_id_question_id_idx ON public.tag_question USING btree (site_id, tag_id, question_id);


--
-- Name: answer answer_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.answer
    ADD CONSTRAINT answer_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: answer answer_site_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.answer
    ADD CONSTRAINT answer_site_id_fkey1 FOREIGN KEY (site_id, owner_user_id) REFERENCES public.so_user(site_id, id);


--
-- Name: answer answer_site_id_fkey2; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.answer
    ADD CONSTRAINT answer_site_id_fkey2 FOREIGN KEY (site_id, last_editor_id) REFERENCES public.so_user(site_id, id);


--
-- Name: answer answer_site_id_fkey3; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.answer
    ADD CONSTRAINT answer_site_id_fkey3 FOREIGN KEY (site_id, question_id) REFERENCES public.question(site_id, id);


--
-- Name: badge badge_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.badge
    ADD CONSTRAINT badge_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: badge badge_site_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.badge
    ADD CONSTRAINT badge_site_id_fkey1 FOREIGN KEY (site_id, user_id) REFERENCES public.so_user(site_id, id);


--
-- Name: comment comment_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.comment
    ADD CONSTRAINT comment_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: post_link post_link_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.post_link
    ADD CONSTRAINT post_link_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: post_link post_link_site_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.post_link
    ADD CONSTRAINT post_link_site_id_fkey1 FOREIGN KEY (site_id, post_id_to) REFERENCES public.question(site_id, id);


--
-- Name: post_link post_link_site_id_fkey2; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.post_link
    ADD CONSTRAINT post_link_site_id_fkey2 FOREIGN KEY (site_id, post_id_from) REFERENCES public.question(site_id, id);


--
-- Name: question question_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.question
    ADD CONSTRAINT question_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: question question_site_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.question
    ADD CONSTRAINT question_site_id_fkey1 FOREIGN KEY (site_id, owner_user_id) REFERENCES public.so_user(site_id, id);


--
-- Name: question question_site_id_fkey2; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.question
    ADD CONSTRAINT question_site_id_fkey2 FOREIGN KEY (site_id, last_editor_id) REFERENCES public.so_user(site_id, id);


--
-- Name: so_user so_user_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.so_user
    ADD CONSTRAINT so_user_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.account(id);


--
-- Name: so_user so_user_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.so_user
    ADD CONSTRAINT so_user_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: tag_question tag_question_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.tag_question
    ADD CONSTRAINT tag_question_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: tag_question tag_question_site_id_fkey1; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.tag_question
    ADD CONSTRAINT tag_question_site_id_fkey1 FOREIGN KEY (site_id, tag_id) REFERENCES public.tag(site_id, id);


--
-- Name: tag_question tag_question_site_id_fkey2; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.tag_question
    ADD CONSTRAINT tag_question_site_id_fkey2 FOREIGN KEY (site_id, question_id) REFERENCES public.question(site_id, id) ON DELETE CASCADE;


--
-- Name: tag tag_site_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: so
--

ALTER TABLE ONLY public.tag
    ADD CONSTRAINT tag_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.site(site_id);


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: postgres
--

REVOKE USAGE ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO PUBLIC;


--
-- PostgreSQL database dump complete
--

