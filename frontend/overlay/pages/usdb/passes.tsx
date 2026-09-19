import type { NextPage } from 'next';
import dynamic from 'next/dynamic';
import React from 'react';

import PageNextJs from 'nextjs/PageNextJs';

const Passes = dynamic(() => import('ui/usdb/Passes'), { ssr: false });

const Page: NextPage = () => <PageNextJs pathname="/usdb/passes"><Passes/></PageNextJs>;

export default Page;
export { base as getServerSideProps } from 'nextjs/getServerSideProps/main';
