import type { NextPage } from 'next';
import dynamic from 'next/dynamic';
import React from 'react';

import PageNextJs from 'nextjs/PageNextJs';

const Economics = dynamic(() => import('ui/usdb/Economics'), { ssr: false });
const Page: NextPage = () => <PageNextJs pathname="/usdb/economics"><Economics/></PageNextJs>;

export default Page;
export { base as getServerSideProps } from 'nextjs/getServerSideProps/main';
