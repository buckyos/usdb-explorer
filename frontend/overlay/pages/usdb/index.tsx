import type { NextPage } from 'next';
import dynamic from 'next/dynamic';
import React from 'react';

import PageNextJs from 'nextjs/PageNextJs';

const Overview = dynamic(() => import('ui/usdb/Overview'), { ssr: false });

const Page: NextPage = () => <PageNextJs pathname="/usdb"><Overview/></PageNextJs>;

export default Page;
export { base as getServerSideProps } from 'nextjs/getServerSideProps/main';
